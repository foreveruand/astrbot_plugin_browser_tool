"""astrbot_plugin_browser_tool — main entry point.

Exposes a single LLM tool ``browse_webpage`` that allows the model to drive a
real browser (Playwright) for both local and remote (WS/CDP) backends.
"""

from __future__ import annotations

import json as _json

import mcp.types

from astrbot.api import AstrBotConfig, llm_tool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .core.browser_manager import BrowserActions, BrowserManager

# Actions that require a currently open page (session must be started with goto first)
_STATEFUL_ACTIONS = {
    "get_content",
    "screenshot",
    "click",
    "fill",
    "select",
    "evaluate",
    "wait",
}
# Actions that need a url parameter
_URL_REQUIRED_ACTIONS = {"goto"}
# Actions that need selector
_SELECTOR_REQUIRED_ACTIONS = {"click", "fill", "select", "wait"}


class Main(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self._browser_manager: BrowserManager | None = None
        self._actions: BrowserActions | None = None

    async def initialize(self) -> None:
        """Start browser manager and optionally customize tool description."""
        self._browser_manager = BrowserManager(dict(self.config))

        max_content = self.config.get("runtime_config", {}).get(
            "max_content_length", 8000
        )
        screenshot_max = self.config.get("runtime_config", {}).get(
            "screenshot_max_size_kb", 200
        )
        self._actions = BrowserActions(
            max_content_length=max_content,
            screenshot_max_size_kb=screenshot_max,
        )

        # Rewrite tool description from config if provided.
        tool_mgr = self.context.get_llm_tool_manager()
        tool = tool_mgr.get_func("browse_webpage")
        if tool:
            tool_cfg = self.config.get("tool_config", {})
            desc = tool_cfg.get("tool_description", "").strip()
            if desc:
                tool.description = desc

        mode = self.config.get("browser_config", {}).get("connection_mode", "local")
        logger.info(f"[browser_tool] Plugin initialized. connection_mode={mode}")

    async def terminate(self) -> None:
        """Gracefully close all open browser sessions."""
        if self._browser_manager is not None:
            await self._browser_manager.close_all()
        logger.info("[browser_tool] Plugin terminated, all sessions closed.")

    # ──────────────────────── LLM tool ────────────────────────────────────

    @llm_tool(name="browse_webpage_tool")
    async def browse_webpage(
        self,
        event: AstrMessageEvent,
        action: str,
        url: str = "",
        selector: str = "",
        value: str = "",
        script: str = "",
        content_type: str = "text",
        timeout: int = 0,
    ) -> str | mcp.types.CallToolResult:
        """Control a real browser to interact with web pages.

        The browser session persists across multiple calls in the same conversation.
        Start a session by calling with action='goto', then use other actions to
        interact with the loaded page without specifying url again.

        Available actions:
        - goto: Navigate to url. Returns page title, visible text summary, links and form elements.
        - get_content: Get page text (content_type='text') or raw HTML (content_type='html').
        - screenshot: Take a JPEG screenshot. The image is returned directly; use send_message_to_user with the provided path to share it.
        - click: Click the element matched by selector.
        - fill: Type value into the input matched by selector.
        - select: Choose a dropdown option by value in the element matched by selector.
        - evaluate: Execute JavaScript (script) and return the result.
        - wait: Wait until selector appears on the page.

        The browser session persists until the user explicitly closes it with /browser_close or it times out from inactivity.
        Do NOT close the session yourself between steps — keep it open for the entire multi-step task.

        Args:
            action(string): The browser action to perform. One of: goto, get_content, screenshot, click, fill, select, evaluate, wait.
            url(string): Target URL. Required for action='goto'.
            selector(string): CSS or Playwright text selector (e.g. '#submit', 'text=Login'). Required for click, fill, select, wait.
            value(string): Value to fill into an input or option to select. Required for fill and select.
            script(string): JavaScript code to evaluate on the page. Required for evaluate.
            content_type(string): 'text' for readable text or 'html' for raw HTML source. Used with get_content.
            timeout(number): Per-action timeout in seconds. 0 means use the plugin default.
        """
        only_admin = self.config.get("tool_config", {}).get("only_admin", True)
        if only_admin and not event.is_admin:
            return json_err("Permission denied: this tool is restricted to admins.")

        if self._browser_manager is None or self._actions is None:
            return json_err("Plugin not fully initialized. Please retry in a moment.")

        default_timeout = self.config.get("runtime_config", {}).get(
            "default_timeout", 30
        )
        effective_timeout = timeout if timeout and timeout > 0 else default_timeout

        action = action.strip().lower()

        # ── validate action ───────────────────────────────────────────────
        valid_actions = {
            "goto",
            "get_content",
            "screenshot",
            "click",
            "fill",
            "select",
            "evaluate",
            "wait",
        }
        if action not in valid_actions:
            return json_err(
                f"Unknown action '{action}'. Valid: {', '.join(sorted(valid_actions))}"
            )

        if action in _URL_REQUIRED_ACTIONS and not url:
            return json_err(f"action='{action}' requires the 'url' parameter.")

        if action in _SELECTOR_REQUIRED_ACTIONS and not selector:
            return json_err(f"action='{action}' requires the 'selector' parameter.")

        if action == "fill" and not value:
            return json_err("action='fill' requires the 'value' parameter.")

        if action == "select" and not value:
            return json_err("action='select' requires the 'value' parameter.")

        if action == "evaluate" and not script:
            return json_err("action='evaluate' requires the 'script' parameter.")

        # ── ensure session for stateful actions ───────────────────────────
        if action in _STATEFUL_ACTIONS:
            key = event.unified_msg_origin
            try:
                session = await self._browser_manager.get_or_create_session(key)
            except Exception as exc:
                logger.error(
                    f"[browser_tool] Failed to create session: {exc}", exc_info=True
                )
                return f"[browser_tool] Failed to open browser: {exc}"

            # Check whether we actually have an active page (non-goto stateful actions
            # need a page that was previously navigated to).
            try:
                current_url = session.page.url
                if current_url in ("about:blank", "") and action != "goto":
                    return json_err(
                        f"action='{action}' requires an active page. "
                        "Call action='goto' first to navigate to a URL."
                    )
            except Exception:
                return json_err("No active browser page. Call action='goto' first.")

        try:
            key = event.unified_msg_origin
            session = await self._browser_manager.get_or_create_session(key)
            page = session.page

            if action == "goto":
                result_str = await self._actions.action_goto(
                    page, url, effective_timeout
                )
            elif action == "get_content":
                result_str = await self._actions.action_get_content(page, content_type)
            elif action == "screenshot":
                result_str = await self._actions.action_screenshot(page)
            elif action == "click":
                result_str = await self._actions.action_click(
                    page, selector, effective_timeout
                )
            elif action == "fill":
                result_str = await self._actions.action_fill(
                    page, selector, value, effective_timeout
                )
            elif action == "select":
                result_str = await self._actions.action_select(
                    page, selector, value, effective_timeout
                )
            elif action == "evaluate":
                result_str = await self._actions.action_evaluate(page, script)
            elif action == "wait":
                result_str = await self._actions.action_wait(
                    page, selector, effective_timeout
                )
            else:
                result_str = json_err(f"Unhandled action: '{action}'.")

            session.touch()
            return result_str

        except Exception as exc:
            logger.error(
                f"[browser_tool] action='{action}' failed: {exc}", exc_info=True
            )
            return json_err(f"Browser action '{action}' failed: {exc}")

    # ──────────────────────── admin command ───────────────────────────────

    @filter.command("browser_close")
    async def browser_close(self, event: AstrMessageEvent):
        """Close your active browser session. Usage: /browser_close"""
        if not event.is_admin:
            await event.send(event.plain_result("Permission denied: admins only."))
            return
        if self._browser_manager is None:
            await event.send(
                event.plain_result("[browser_tool] Plugin not initialized.")
            )
            return
        key = event.unified_msg_origin
        closed = await self._browser_manager.close_session(key)
        if closed:
            await event.send(
                event.plain_result("[browser_tool] Browser session closed.")
            )
        else:
            await event.send(
                event.plain_result("[browser_tool] No active browser session.")
            )

    @filter.command("browser_status")
    async def browser_status(self, event: AstrMessageEvent):
        """Show active browser sessions (admin only). Usage: /browser_status"""
        if not event.is_admin:
            await event.send(event.plain_result("Permission denied: admins only."))
            return
        if self._browser_manager is None:
            await event.send(
                event.plain_result("[browser_tool] Plugin not initialized.")
            )
            return
        sessions = self._browser_manager._sessions
        if not sessions:
            await event.send(
                event.plain_result("[browser_tool] No active browser sessions.")
            )
            return
        lines = [f"Active browser sessions ({len(sessions)}):"]
        for k in sessions:
            lines.append(f"  • {k}")
        await event.send(event.plain_result("\n".join(lines)))


# ──────────────────────────── helpers ───────────────────────────────────────


def _to_json(obj: object) -> str:
    return _json.dumps(obj, ensure_ascii=False, indent=2)


def json_err(message: str) -> str:
    return _to_json({"success": False, "error": message})
