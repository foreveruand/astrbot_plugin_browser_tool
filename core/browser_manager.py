"""Browser lifecycle manager for astrbot_plugin_browser_tool.

Handles Playwright browser/context/page creation for both local and remote
(WS/CDP) backends. Each AstrBot session (keyed by unified_msg_origin) gets its
own browser context so conversations are isolated. Idle sessions are evicted
based on configurable TTL.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger

# ──────────────────────────── session state ────────────────────────────────


@dataclass
class BrowserSession:
    """Per-conversation Playwright state."""

    browser: Any  # playwright Browser or BrowserContext depending on remote mode
    context: Any  # BrowserContext
    page: Any  # Page (most recent active page)
    last_used: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        self.last_used = time.monotonic()

    async def close(self) -> None:
        try:
            await self.page.close()
        except Exception:
            pass
        try:
            await self.context.close()
        except Exception:
            pass
        # Only close the browser object when it was locally launched.
        # For remote connections the browser object is a connected Browser and
        # closing it would disconnect from the remote endpoint but NOT shut
        # down the remote process — still safe to call .close() on it.
        try:
            await self.browser.close()
        except Exception:
            pass


# ──────────────────────────── manager ───────────────────────────────────────


class BrowserManager:
    """Create, cache, and evict Playwright browser sessions.

    One BrowserManager instance should be shared for the lifetime of the plugin.
    Call ``close_all()`` in the plugin's ``terminate()`` method.
    """

    def __init__(self, config: dict) -> None:
        self._config = config
        # session_key → BrowserSession
        self._sessions: dict[str, BrowserSession] = {}
        self._lock = asyncio.Lock()
        self._eviction_task: asyncio.Task | None = None

        ttl = self._get("runtime_config", "session_idle_ttl", 600)
        if ttl and ttl > 0:
            self._eviction_task = asyncio.create_task(
                self._eviction_loop(ttl), name="browser_session_eviction"
            )

    # ── public API ────────────────────────────────────────────────────────

    async def get_or_create_session(self, key: str) -> BrowserSession:
        """Return existing live session or start a new browser for *key*."""
        async with self._lock:
            existing = self._sessions.get(key)
            if existing is not None:
                try:
                    # Quick liveness check — throws if page is closed/crashed.
                    await existing.page.title()
                    existing.touch()
                    return existing
                except Exception:
                    logger.debug(
                        f"[browser_tool] Session '{key}' became stale, recreating."
                    )
                    await self._close_session_unlocked(key)

            session = await self._create_session()
            self._sessions[key] = session
            return session

    async def close_session(self, key: str) -> bool:
        """Explicitly close a session. Returns True if it existed."""
        async with self._lock:
            return await self._close_session_unlocked(key)

    async def close_all(self) -> None:
        """Close every open session and cancel the eviction task."""
        if self._eviction_task is not None:
            self._eviction_task.cancel()
            try:
                await self._eviction_task
            except asyncio.CancelledError:
                pass

        async with self._lock:
            keys = list(self._sessions.keys())
            for k in keys:
                await self._close_session_unlocked(k)

    # ── internal helpers ─────────────────────────────────────────────────

    async def _close_session_unlocked(self, key: str) -> bool:
        session = self._sessions.pop(key, None)
        if session is None:
            return False
        await session.close()
        logger.debug(f"[browser_tool] Closed session '{key}'.")
        return True

    async def _create_session(self) -> BrowserSession:
        """Instantiate a new Playwright browser + context + page."""
        from playwright.async_api import async_playwright

        mode = self._get("browser_config", "connection_mode", "local")
        timeout_sec = self._get("runtime_config", "default_timeout", 30)
        viewport_w = self._get("runtime_config", "viewport_width", 1280)
        viewport_h = self._get("runtime_config", "viewport_height", 720)
        user_agent = self._get("runtime_config", "user_agent", "")
        storage_state = self._get("runtime_config", "storage_state_path", "")
        proxy_cfg = self._build_proxy_config()

        playwright = await async_playwright().start()

        context_kwargs: dict[str, Any] = {
            "viewport": {"width": viewport_w, "height": viewport_h},
        }
        if user_agent:
            context_kwargs["user_agent"] = user_agent
        if proxy_cfg:
            context_kwargs["proxy"] = proxy_cfg
        if storage_state:
            context_kwargs["storage_state"] = storage_state

        if mode == "remote":
            browser, context = await self._connect_remote(playwright, context_kwargs)
        else:
            browser, context = await self._launch_local(playwright, context_kwargs)

        page = await context.new_page()
        page.set_default_timeout(timeout_sec * 1000)
        return BrowserSession(browser=browser, context=context, page=page)

    # ── local launch ──────────────────────────────────────────────────────

    async def _launch_local(self, playwright: Any, context_kwargs: dict) -> tuple:
        browser_type_name = self._get("browser_config", "browser_type", "chromium")
        headless = self._get("browser_config", "headless", True)
        executable = self._get("browser_config", "browser_executable_path", "")
        channel = self._get("browser_config", "browser_channel", "")
        raw_args = self._get("browser_config", "launch_args", "")

        launch_kwargs: dict[str, Any] = {"headless": headless}
        if executable:
            launch_kwargs["executable_path"] = executable
        if channel:
            launch_kwargs["channel"] = channel
        if raw_args:
            launch_kwargs["args"] = [a.strip() for a in raw_args.split() if a.strip()]

        proxy_cfg = self._build_proxy_config()
        if proxy_cfg:
            launch_kwargs["proxy"] = proxy_cfg
            # Remove from context_kwargs to avoid duplication when using local launch.
            context_kwargs.pop("proxy", None)

        try:
            browser_type = getattr(playwright, browser_type_name)
        except AttributeError:
            raise ValueError(
                f"[browser_tool] Unsupported browser_type: '{browser_type_name}'. "
                "Choose from: chromium, firefox, webkit."
            )

        logger.info(
            f"[browser_tool] Launching local {browser_type_name} "
            f"(headless={headless}, executable='{executable or 'auto'}')."
        )
        browser = await browser_type.launch(**launch_kwargs)
        context = await browser.new_context(**context_kwargs)
        return browser, context

    # ── remote connect ────────────────────────────────────────────────────

    async def _connect_remote(self, playwright: Any, context_kwargs: dict) -> tuple:
        ws_endpoint = self._get("browser_config", "remote_ws_endpoint", "")
        cdp_url = self._get("browser_config", "remote_cdp_url", "")
        connect_timeout = (
            self._get("browser_config", "remote_connect_timeout", 30) * 1000
        )

        if not ws_endpoint and not cdp_url:
            raise ValueError(
                "[browser_tool] remote mode requires either remote_ws_endpoint or "
                "remote_cdp_url to be configured."
            )

        if ws_endpoint:
            logger.info(
                f"[browser_tool] Connecting to remote browser via WS: {ws_endpoint}"
            )
            browser = await playwright.chromium.connect(
                ws_endpoint, timeout=connect_timeout
            )
        else:
            logger.info(
                f"[browser_tool] Connecting to remote browser via CDP: {cdp_url}"
            )
            browser = await playwright.chromium.connect_over_cdp(
                cdp_url, timeout=connect_timeout
            )

        # For remote connections Playwright recommends creating a new context.
        context = await browser.new_context(**context_kwargs)
        return browser, context

    # ── proxy builder ─────────────────────────────────────────────────────

    def _build_proxy_config(self) -> dict | None:
        server = self._get("runtime_config", "proxy_server", "")
        if not server:
            return None
        cfg: dict[str, str] = {"server": server}
        username = self._get("runtime_config", "proxy_username", "")
        password = self._get("runtime_config", "proxy_password", "")
        if username:
            cfg["username"] = username
        if password:
            cfg["password"] = password
        return cfg

    # ── config helper ─────────────────────────────────────────────────────

    def _get(self, group: str, key: str, default: Any = None) -> Any:
        return self._config.get(group, {}).get(key, default)

    # ── eviction loop ─────────────────────────────────────────────────────

    async def _eviction_loop(self, ttl: int) -> None:
        """Periodically close sessions that have been idle longer than *ttl* seconds."""
        check_interval = max(60, ttl // 4)
        while True:
            await asyncio.sleep(check_interval)
            now = time.monotonic()
            async with self._lock:
                stale_keys = [
                    k for k, s in self._sessions.items() if (now - s.last_used) > ttl
                ]
            for k in stale_keys:
                logger.info(f"[browser_tool] Evicting idle session '{k}'.")
                async with self._lock:
                    await self._close_session_unlocked(k)


# ──────────────────────────── actions ───────────────────────────────────────


MAX_LINKS = 30
MAX_FORM_ELEMENTS = 30


class BrowserActions:
    """Stateless action implementations that operate on a Playwright Page."""

    def __init__(self, max_content_length: int, screenshot_max_size_kb: int) -> None:
        self.max_content_length = max_content_length
        self.screenshot_max_size_kb = screenshot_max_size_kb

    # ── goto ──────────────────────────────────────────────────────────────

    async def action_goto(self, page: Any, url: str, timeout: int) -> str:
        response = await page.goto(
            url, wait_until="domcontentloaded", timeout=timeout * 1000
        )
        try:
            await page.wait_for_load_state(
                "networkidle", timeout=min(timeout, 15) * 1000
            )
        except Exception:
            pass  # networkidle timeout is non-fatal

        status = response.status if response else "unknown"
        title = await page.title()
        page_url = page.url

        text_content = await page.inner_text("body")
        if text_content and len(text_content) > self.max_content_length:
            text_content = (
                text_content[: self.max_content_length] + "\n\n...(content truncated)"
            )

        links = await page.evaluate("""
            () => {
                const links = [];
                document.querySelectorAll('a[href]').forEach(a => {
                    const text = a.innerText.trim();
                    const href = a.href;
                    if (text && href && !href.startsWith('javascript:')) {
                        links.push({text: text.substring(0, 80), href: href});
                    }
                });
                return links.slice(0, 30);
            }
        """)

        forms = await page.evaluate("""
            () => {
                const forms = [];
                document.querySelectorAll('input, textarea, select, button').forEach(el => {
                    const info = {
                        tag: el.tagName.toLowerCase(),
                        type: el.type || '',
                        name: el.name || '',
                        id: el.id || '',
                        placeholder: el.placeholder || '',
                        value: el.tagName.toLowerCase() === 'select'
                            ? '' : (el.value || '').substring(0, 50),
                        text: el.innerText ? el.innerText.trim().substring(0, 50) : ''
                    };
                    if (info.name || info.id || info.placeholder || info.text) {
                        forms.push(info);
                    }
                });
                return forms.slice(0, 30);
            }
        """)

        result: dict = {
            "status": status,
            "url": page_url,
            "title": title,
            "text_content": text_content,
        }
        if links:
            result["links"] = links
        if forms:
            result["form_elements"] = forms

        return json.dumps(result, ensure_ascii=False, indent=2)

    # ── get_content ───────────────────────────────────────────────────────

    async def action_get_content(self, page: Any, content_type: str) -> str:
        title = await page.title()
        page_url = page.url

        if content_type == "html":
            content = await page.content()
        else:
            content = await page.inner_text("body")

        if content and len(content) > self.max_content_length:
            content = content[: self.max_content_length] + "\n\n...(content truncated)"

        return json.dumps(
            {
                "url": page_url,
                "title": title,
                "content_type": content_type,
                "content": content,
            },
            ensure_ascii=False,
            indent=2,
        )

    # ── screenshot ────────────────────────────────────────────────────────

    async def action_screenshot(self, page: Any) -> str:
        max_bytes = self.screenshot_max_size_kb * 1024

        screenshot_bytes = await page.screenshot(
            full_page=False, type="jpeg", quality=70
        )
        b64 = base64.b64encode(screenshot_bytes).decode("utf-8")

        if len(b64) > max_bytes:
            screenshot_bytes = await page.screenshot(
                full_page=False, type="jpeg", quality=35
            )
            b64 = base64.b64encode(screenshot_bytes).decode("utf-8")

        return json.dumps(
            {
                "url": page.url,
                "title": await page.title(),
                "screenshot_base64": b64,
                "format": "jpeg",
                "note": "Screenshot returned as base64-encoded JPEG.",
            },
            ensure_ascii=False,
            indent=2,
        )

    # ── click ─────────────────────────────────────────────────────────────

    async def action_click(self, page: Any, selector: str, timeout: int) -> str:
        await page.click(selector, timeout=timeout * 1000)
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass

        return json.dumps(
            {
                "success": True,
                "message": f"Clicked element: {selector}",
                "current_url": page.url,
                "current_title": await page.title(),
            },
            ensure_ascii=False,
            indent=2,
        )

    # ── fill ──────────────────────────────────────────────────────────────

    async def action_fill(
        self, page: Any, selector: str, value: str, timeout: int
    ) -> str:
        await page.fill(selector, value, timeout=timeout * 1000)
        return json.dumps(
            {
                "success": True,
                "message": f"Filled '{selector}' with value.",
            },
            ensure_ascii=False,
            indent=2,
        )

    # ── select ────────────────────────────────────────────────────────────

    async def action_select(
        self, page: Any, selector: str, value: str, timeout: int
    ) -> str:
        await page.select_option(selector, value=value, timeout=timeout * 1000)
        return json.dumps(
            {
                "success": True,
                "message": f"Selected option '{value}' in '{selector}'.",
            },
            ensure_ascii=False,
            indent=2,
        )

    # ── evaluate ──────────────────────────────────────────────────────────

    async def action_evaluate(self, page: Any, script: str) -> str:
        result = await page.evaluate(script)

        if result is None:
            formatted = "null"
        elif isinstance(result, (dict, list)):
            formatted = json.dumps(result, ensure_ascii=False, indent=2)
        else:
            formatted = str(result)

        if len(formatted) > self.max_content_length:
            formatted = (
                formatted[: self.max_content_length] + "\n\n...(result truncated)"
            )

        return json.dumps(
            {"success": True, "result": formatted},
            ensure_ascii=False,
            indent=2,
        )

    # ── wait ──────────────────────────────────────────────────────────────

    async def action_wait(self, page: Any, selector: str, timeout: int) -> str:
        element = await page.wait_for_selector(selector, timeout=timeout * 1000)
        if element:
            visible = await element.is_visible()
            text = await element.inner_text()
            if text and len(text) > 200:
                text = text[:200] + "..."
            return json.dumps(
                {
                    "success": True,
                    "message": f"Element '{selector}' appeared.",
                    "visible": visible,
                    "text": text,
                },
                ensure_ascii=False,
                indent=2,
            )
        return json.dumps(
            {"success": False, "message": f"Timed out waiting for '{selector}'."},
            ensure_ascii=False,
            indent=2,
        )
