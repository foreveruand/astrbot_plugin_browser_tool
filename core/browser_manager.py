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

import mcp.types

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
        # Persist the last successfully navigated URL per session key so that
        # when the remote browser (e.g. browserless.io) kills the connection
        # we can silently reconnect and restore the page.
        self._last_known_urls: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._eviction_task: asyncio.Task | None = None

        ttl = self._get("runtime_config", "session_idle_ttl", 600)
        if ttl and ttl > 0:
            self._eviction_task = asyncio.create_task(
                self._eviction_loop(ttl), name="browser_session_eviction"
            )

    # ── public API ────────────────────────────────────────────────────────

    async def get_or_create_session(self, key: str) -> BrowserSession:
        """Return existing live session or start a new browser for *key*.

        If the previous session is detected as stale (e.g. the remote endpoint
        timed out and killed the CDP connection), the last known URL is saved
        and the new session automatically navigates back to it so the LLM can
        continue without noticing the reconnection.
        """
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
                    # Preserve the last URL before tearing down the dead session.
                    try:
                        last_url = existing.page.url
                        if last_url and last_url not in ("about:blank", ""):
                            self._last_known_urls[key] = last_url
                            logger.info(
                                f"[browser_tool] Saved last URL for '{key}': {last_url}"
                            )
                    except Exception:
                        pass
                    await self._close_session_unlocked(key)

            session = await self._create_session()
            self._sessions[key] = session

        # Auto-reconnect: navigate back to the last known URL outside the lock
        # so we don't hold it during a potentially slow network request.
        reconnect_url = self._last_known_urls.get(key, "")
        if reconnect_url:
            logger.info(
                f"[browser_tool] Auto-reconnecting '{key}' to last URL: {reconnect_url}"
            )
            try:
                await session.page.goto(
                    reconnect_url, wait_until="domcontentloaded"
                )
                logger.info("[browser_tool] Auto-reconnect navigation successful.")
            except Exception as exc:
                logger.warning(
                    f"[browser_tool] Auto-reconnect to '{reconnect_url}' failed: {exc}. "
                    "LLM will need to call goto again."
                )
                # Clear the stale URL so we don't loop on a permanently broken URL.
                self._last_known_urls.pop(key, None)

        return session

    def set_last_url(self, key: str, url: str) -> None:
        """Record the last successfully navigated URL for *key*.

        Call this after every successful goto so auto-reconnect always has a
        valid destination.
        """
        if url and url not in ("about:blank", ""):
            self._last_known_urls[key] = url

    async def close_session(self, key: str) -> bool:
        """Explicitly close a session and clear its saved URL. Returns True if it existed."""
        async with self._lock:
            self._last_known_urls.pop(key, None)
            return await self._close_session_unlocked(key)

    async def close_all(self) -> None:
        """Close every open session and cancel the eviction task."""
        if self._eviction_task is not None:
            self._eviction_task.cancel()
            try:
                await self._eviction_task
            except asyncio.CancelledError:
                pass

        self._last_known_urls.clear()
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
        # "cdp" (default) — Chrome DevTools Protocol, suits browserless.io / headless Chrome.
        # "playwright_server" — Playwright built-in server started with `playwright run-server`.
        remote_protocol = self._get("browser_config", "remote_protocol", "cdp")
        connect_timeout = (
            self._get("browser_config", "remote_connect_timeout", 30) * 1000
        )

        endpoint = ws_endpoint or cdp_url
        if not endpoint:
            raise ValueError(
                "[browser_tool] remote mode requires either remote_ws_endpoint or "
                "remote_cdp_url to be configured."
            )

        if remote_protocol == "playwright_server":
            # Playwright's own server protocol (playwright run-server).
            # Only accepts WS endpoints; cdp_url is not applicable here.
            target = ws_endpoint
            if not target:
                raise ValueError(
                    "[browser_tool] remote_protocol='playwright_server' requires "
                    "remote_ws_endpoint to be set."
                )
            logger.info(f"[browser_tool] Connecting to Playwright server: {target}")
            browser = await playwright.chromium.connect(target, timeout=connect_timeout)
        else:
            # CDP mode — works for browserless.io, headless Chrome, and any
            # service that exposes a Chrome DevTools Protocol WebSocket or HTTP
            # endpoint. Both ws:// and http:// URLs are accepted by connect_over_cdp.
            logger.info(
                f"[browser_tool] Connecting via CDP ({remote_protocol}): {endpoint}"
            )
            browser = await playwright.chromium.connect_over_cdp(
                endpoint, timeout=connect_timeout
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
                # Clear saved URL on idle eviction — user starts fresh next time.
                self._last_known_urls.pop(k, None)


# ──────────────────────────── actions ───────────────────────────────────────


MAX_LINKS = 30
MAX_FORM_ELEMENTS = 30


class BrowserActions:
    """Stateless action implementations that operate on a Playwright Page."""

    def __init__(self, max_content_length: int, screenshot_max_size_kb: int) -> None:
        self.max_content_length = max_content_length
        self.screenshot_max_size_kb = screenshot_max_size_kb

    # ── goto ──────────────────────────────────────────────────────────────

    # Keywords that indicate Cloudflare is blocking the page.
    _CF_SIGNALS = (
        "challenges.cloudflare.com",
        "cf-turnstile",
        "cf_chl_opt",
        "Checking your browser",
        "DDoS protection",
        "Ray ID",
        "https://challenges.cloudflare.com",
    )

    async def _is_cloudflare_challenge(self, page: Any) -> bool:
        """Return True if the current page appears to be a CF challenge / block."""
        try:
            html = await page.content()
            return any(sig in html for sig in self._CF_SIGNALS)
        except Exception:
            return False

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

        # ── Cloudflare auto-handling ───────────────────────────────────────
        cf_result: str | None = None
        if await self._is_cloudflare_challenge(page):
            logger.info(
                "[browser_tool] Cloudflare challenge detected after goto — "
                "attempting automatic handling."
            )
            try:
                # Wait briefly for the Turnstile iframe/widget to fully render.
                await asyncio.sleep(2.0)
                cf_outcome = await self.action_cloudflare_click(page)
                if isinstance(cf_outcome, str):
                    import json as _j
                    parsed = _j.loads(cf_outcome)
                    if parsed.get("success"):
                        cf_result = "cloudflare_auto_passed"
                    else:
                        cf_result = "cloudflare_auto_failed"
                else:
                    # CallToolResult with screenshot — challenge not resolved
                    cf_result = "cloudflare_auto_failed"
            except Exception as exc:
                logger.warning(f"[browser_tool] CF auto-handling error: {exc}")
                cf_result = "cloudflare_auto_failed"

        status = response.status if response else "unknown"
        title = await page.title()
        page_url = page.url

        # If CF was handled successfully, refresh page state from the real page.
        if cf_result == "cloudflare_auto_passed":
            try:
                await page.wait_for_load_state(
                    "networkidle", timeout=min(timeout, 10) * 1000
                )
            except Exception:
                pass
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
        if cf_result:
            result["cloudflare_handled"] = cf_result
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

    async def action_screenshot(self, page: Any) -> mcp.types.CallToolResult:
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

        info = json.dumps(
            {"url": page.url, "title": await page.title()},
            ensure_ascii=False,
        )
        return mcp.types.CallToolResult(
            content=[
                mcp.types.TextContent(type="text", text=info),
                mcp.types.ImageContent(type="image", data=b64, mimeType="image/jpeg"),
            ]
        )

    # ── click ─────────────────────────────────────────────────────────────

    async def action_click(
        self,
        page: Any,
        selector: str,
        timeout: int,
        x: float = 0,
        y: float = 0,
    ) -> str:
        """Click either at absolute coordinates (x, y) or at the center of the
        element found by *selector*.

        Using page.mouse.click() instead of page.click() sends raw OS-level mouse
        events and intentionally bypasses Playwright's actionability checks (visible,
        stable, enabled, etc.). This lets the LLM interact with elements that are
        inside cross-origin iframes (e.g. Cloudflare CAPTCHA) or are otherwise
        blocked by the high-level click API.
        """
        if x or y:
            # Direct coordinate click — LLM determined the position via screenshot.
            await page.mouse.click(x, y)
            click_desc = f"coordinate ({x}, {y})"
        else:
            # Locate element, compute its center, then fire a raw mouse click.
            locator = page.locator(selector)
            try:
                await locator.wait_for(state="visible", timeout=timeout * 1000)
            except Exception:
                # Element may be in an iframe or otherwise visibility-check-exempt;
                # try to get the bounding box even if visibility wait failed.
                pass

            box = await locator.bounding_box()
            if box is None:
                # Graceful fallback: try page.click() — works for plain elements.
                await page.click(selector, timeout=timeout * 1000)
                click_desc = f"selector '{selector}' (fallback page.click)"
            else:
                cx = box["x"] + box["width"] / 2
                cy = box["y"] + box["height"] / 2
                await page.mouse.click(cx, cy)
                click_desc = f"selector '{selector}' at ({cx:.0f}, {cy:.0f})"

        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass

        return json.dumps(
            {
                "success": True,
                "message": f"Clicked {click_desc}.",
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

    # ── cloudflare_click ──────────────────────────────────────────────────

    async def action_cloudflare_click(self, page: Any) -> str | mcp.types.CallToolResult:
        """Attempt to automatically solve a Cloudflare Turnstile / challenge page.

        Strategy (based on reverse-engineering CF's bot detection):
        1. Locate the CF Turnstile wrapper on the main page.
        2. Dispatch raw CDP ``Input.dispatchMouseEvent`` with ``screenX != clientX``
           (real mice always differ because screenX includes the window chrome offset
           — CDP's page.click() erroneously sends screenX == clientX, which CF flags).
        3. Simulate a natural mouse trajectory with smooth-step easing and micro-jitter.
        4. Wait 3 s and check if the challenge widget disappeared.
        5. On any failure or if the widget is not found, take a screenshot and return
           it with the full viewport resolution so the LLM can identify coordinates
           and call action='click' with x/y parameters instead.
        """
        import asyncio
        import random

        # Selectors that identify the CF challenge widget on the host page.
        # The checkbox itself lives inside a shadow root → iframe, but its
        # *wrapper* is accessible and has a known bounding box.
        CF_SELECTORS = [
            "[class*='cf-turnstile']",
            "[id*='cf-turnstile']",
            "#turnstile-wrapper",
            "[class*='turnstile-wrapper']",
            # Older Turnstile / JS challenge page widgets
            "#challenge-form",
            ".ctp-checkbox-label",
            # Generic: any iframe pointing to the CF challenge CDN
            "iframe[src*='challenges.cloudflare.com']",
        ]

        async def _find_box() -> dict | None:
            for sel in CF_SELECTORS:
                try:
                    loc = page.locator(sel).first
                    b = await loc.bounding_box()
                    if b:
                        logger.info(
                            f"[browser_tool] CF element found via '{sel}': {b}"
                        )
                        return b
                except Exception:
                    pass
            return None

        box = await _find_box()

        if box is not None:
            # CF Turnstile checkbox sits at roughly (28 px, center-y) within the
            # widget.  We bias our target toward that position.
            target_x = box["x"] + min(28.0, box["width"] * 0.22)
            target_y = box["y"] + box["height"] * 0.5

            # Simulate a realistic browser window position on a physical screen.
            # A typical desktop has: screenX = clientX + (window left edge offset).
            # We pick a random but plausible offset so screenX != clientX.
            win_x = float(random.randint(80, 280))
            win_y = float(random.randint(80, 150))  # toolbar height ≈ 85-140 px

            try:
                cdp = await page.context.new_cdp_session(page)

                # ── human-like approach trajectory (smooth-step + jitter) ──────
                start_x = target_x + random.choice([-1, 1]) * random.uniform(120, 220)
                start_y = target_y + random.uniform(-60, 60)
                n_steps = random.randint(12, 20)
                for i in range(n_steps + 1):
                    t = i / n_steps
                    ease = t * t * (3.0 - 2.0 * t)  # smooth-step
                    mx = start_x + (target_x - start_x) * ease + random.gauss(0, 1.2)
                    my = start_y + (target_y - start_y) * ease + random.gauss(0, 1.2)
                    await cdp.send(
                        "Input.dispatchMouseEvent",
                        {
                            "type": "mouseMoved",
                            "x": round(mx, 2),
                            "y": round(my, 2),
                            "screenX": round(mx + win_x, 2),
                            "screenY": round(my + win_y, 2),
                            "modifiers": 0,
                            "button": "none",
                            "buttons": 0,
                        },
                    )
                    await asyncio.sleep(random.uniform(0.018, 0.055))

                # Brief human hesitation before clicking
                await asyncio.sleep(random.uniform(0.08, 0.30))

                # Final micro-jitter to land exactly on the checkbox
                final_x = target_x + random.uniform(-4.0, 4.0)
                final_y = target_y + random.uniform(-4.0, 4.0)

                for ev_type in ("mousePressed", "mouseReleased"):
                    await cdp.send(
                        "Input.dispatchMouseEvent",
                        {
                            "type": ev_type,
                            "x": round(final_x, 2),
                            "y": round(final_y, 2),
                            "screenX": round(final_x + win_x, 2),
                            "screenY": round(final_y + win_y, 2),
                            "button": "left",
                            "buttons": 1,
                            "clickCount": 1,
                            "modifiers": 0,
                        },
                    )
                    if ev_type == "mousePressed":
                        await asyncio.sleep(random.uniform(0.06, 0.14))

                # Give CF 3 s to validate and redirect
                await asyncio.sleep(3.0)

                # ── check if challenge disappeared ─────────────────────────────
                box_after = await _find_box()
                if box_after is None:
                    return json.dumps(
                        {
                            "success": True,
                            "message": (
                                "Cloudflare challenge auto-clicked. "
                                "The verification widget is no longer detected — likely passed."
                            ),
                            "current_url": page.url,
                            "current_title": await page.title(),
                        },
                        ensure_ascii=False,
                        indent=2,
                    )

                logger.info(
                    "[browser_tool] CF widget still present after auto-click. Falling back to screenshot."
                )

            except Exception as exc:
                logger.warning(f"[browser_tool] CF auto-click failed: {exc}")
        else:
            logger.info(
                "[browser_tool] No CF challenge widget found on page. "
                "Falling back to screenshot for visual identification."
            )

        # ── fallback: screenshot + resolution info ─────────────────────────────
        vp = page.viewport_size or {}
        vp_w = int(vp.get("width", 1280))
        vp_h = int(vp.get("height", 720))

        screenshot_bytes = await page.screenshot(full_page=False, type="jpeg", quality=75)
        b64 = base64.b64encode(screenshot_bytes).decode("utf-8")

        info = json.dumps(
            {
                "success": False,
                "message": (
                    "Cloudflare challenge auto-click failed or the widget could not be "
                    "located automatically. Review the screenshot below and identify the "
                    "exact pixel position of the verification checkbox. "
                    f"The screenshot resolution is {vp_w}\u00d7{vp_h} px — map the "
                    "checkbox center to (x, y) and call action='click' with those "
                    "coordinates (no selector needed)."
                ),
                "viewport_width": vp_w,
                "viewport_height": vp_h,
                "current_url": page.url,
            },
            ensure_ascii=False,
        )
        return mcp.types.CallToolResult(
            content=[
                mcp.types.TextContent(type="text", text=info),
                mcp.types.ImageContent(type="image", data=b64, mimeType="image/jpeg"),
            ]
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
