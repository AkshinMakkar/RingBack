"""One browser-use session, with frames pushed to WebSocket clients."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from browser_use import Agent, Browser, ChatAnthropic

logger = logging.getLogger("dispatch.agent")

VIEWPORT = {"width": 1280, "height": 720}
FRAME_INTERVAL_S = 0.5
MAX_STEPS = 60
PROFILE_DIR = Path(__file__).resolve().parent / ".browser-profile"


def _system_chrome_path() -> Path | None:
    candidates = [
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path("/usr/bin/google-chrome"),
        Path("/usr/bin/google-chrome-stable"),
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


def _make_browser() -> Browser:
    """Prefer real Chrome. Bundled Chromium gets blocked on a lot of sites."""
    cdp = (os.getenv("CHROME_CDP_URL") or "").strip()
    if cdp:
        logger.info("Attaching to existing Chrome via %s", cdp)
        return Browser(cdp_url=cdp, is_local=True, highlight_elements=True)

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, Any] = {
        "headless": False,
        "viewport": VIEWPORT,
        "window_size": VIEWPORT,
        "keep_alive": True,
        "user_data_dir": str(PROFILE_DIR),
        "highlight_elements": True,
        "wait_between_actions": 0.6,
        "ignore_default_args": ["--enable-automation"],
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--no-first-run",
            "--no-default-browser-check",
        ],
    }
    chrome = _system_chrome_path()
    if chrome is not None:
        kwargs["executable_path"] = str(chrome)
        kwargs["channel"] = "chrome"
        logger.info("Launching system Chrome at %s", chrome)
    else:
        logger.warning("System Chrome not found; falling back to bundled Chromium")
    return Browser(**kwargs)


INTERACT_INSTRUCTIONS = """
You control a real Chrome window. Do the thing the user asked. Don't just look at the page.

You can:
- open the site they named and close cookie banners
- log in if a login form shows up
- fill username, email, password, and next/continue
- open a new message, type what they asked, and hit Send
- check that it actually sent (or that you're logged in) before you stop

Do what they asked. Don't open Facebook, Marketplace, Craigslist, or some other site unless they named it or asked for listings.

If they want listings, used stuff, apartments, cars, furniture, or things for sale: Facebook Marketplace only. Not Craigslist or other classifieds.
If they just said hi or didn't give a real task, don't browse. Stop and say you're waiting.

Never type the strings "x_user" or "x_pass" unless this task actually gave those placeholders.
Don't invent passwords. If you hit a login wall and have no credentials, stay on that page. Someone may log in in this same window. Then keep going. Don't quit just because of a login screen.
If a click fails, try another button or link before you give up.
""".strip()

_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.I)
_PLACEHOLDER_RE = re.compile(r"\b(x_user|x_pass)\b", re.I)
_CLASSIFIEDS_RE = re.compile(
    r"\b(craigslist|kijiji|offerup|ebay|letgo)\b",
    re.I,
)
_LISTING_INTENT_RE = re.compile(
    r"\b(listing|listings|marketplace|for sale|used |apartment|furniture|couch|classifieds|car for)\b",
    re.I,
)

TaskStatus = Literal["running", "done", "error"]
TaskSource = Literal["web", "phone"]


def _ensure_virtual_display() -> Any | None:
    """Headed Chrome needs a display. On Linux with no DISPLAY, start Xvfb."""
    if sys.platform == "darwin" or sys.platform.startswith("win"):
        return None
    if os.environ.get("DISPLAY"):
        return None
    try:
        from pyvirtualdisplay import Display

        display = Display(visible=False, size=(VIEWPORT["width"], VIEWPORT["height"]))
        display.start()
        logger.info("Started Xvfb virtual display")
        return display
    except Exception:
        logger.warning("No DISPLAY and pyvirtualdisplay/Xvfb unavailable; browser may fail to launch")
        return None


def _format_step(agent: Agent) -> str:
    parts: list[str] = []
    try:
        thoughts = agent.history.model_thoughts()
        if thoughts:
            last = thoughts[-1]
            text = (
                getattr(last, "next_goal", None)
                or getattr(last, "thinking", None)
                or getattr(last, "evaluation_previous_goal", None)
                or str(last)
            )
            if text:
                parts.append(str(text).strip())
    except Exception:
        pass
    try:
        actions = agent.history.model_actions()
        if actions:
            parts.append(f"action: {actions[-1]}")
    except Exception:
        pass
    try:
        urls = agent.history.urls()
        if urls and urls[-1]:
            parts.append(f"url: {urls[-1]}")
    except Exception:
        pass
    text = " / ".join(parts) if parts else "Step completed"
    return _redact(text)


def _redact(text: str, extra: list[str] | None = None) -> str:
    secrets = [
        os.getenv("LOGIN_PASSWORD") or "",
        os.getenv("LOGIN_USERNAME") or "",
        *(extra or []),
    ]
    out = text
    for secret in secrets:
        if secret and len(secret) >= 3:
            out = out.replace(secret, "***")
    return out


def _for_speech(text: str, extra: list[str] | None = None) -> str:
    cleaned = _redact(str(text), extra=extra)
    cleaned = _URL_RE.sub("", cleaned)
    cleaned = _PLACEHOLDER_RE.sub("", cleaned)
    cleaned = re.sub(r"[*_`#>\-]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,;:")
    if not cleaned:
        return ""
    if len(cleaned) > 180:
        cleaned = cleaned[:177].rsplit(" ", 1)[0]
    if cleaned[-1] not in ".!?":
        cleaned += "."
    return cleaned[0].upper() + cleaned[1:]


def _similar_speech(a: str, b: str) -> bool:
    na = re.sub(r"[^a-z0-9 ]+", "", a.lower()).strip()
    nb = re.sub(r"[^a-z0-9 ]+", "", b.lower()).strip()
    if not na or not nb:
        return False
    return na == nb or na in nb or nb in na


def _speakable_step(agent: Agent) -> str:
    try:
        thoughts = agent.history.model_thoughts()
        if thoughts:
            last = thoughts[-1]
            text = getattr(last, "next_goal", None) or getattr(last, "memory", None)
            if text:
                return _for_speech(text)
    except Exception:
        pass
    return ""


def _build_sensitive_data() -> dict[str, str]:
    user = (os.getenv("LOGIN_USERNAME") or "").strip()
    pw = (os.getenv("LOGIN_PASSWORD") or "").strip()
    data: dict[str, str] = {}
    if user:
        data["x_user"] = user
    if pw:
        data["x_pass"] = pw
    return data


def _steer_listings(instruction: str) -> str:
    text = instruction.strip()
    if _CLASSIFIEDS_RE.search(text) or _LISTING_INTENT_RE.search(text):
        text += (
            " Use Facebook Marketplace only. Skip Craigslist and other classifieds."
        )
    return text


def _task_text(instruction: str, sensitive: dict[str, str], source: TaskSource) -> str:
    extra = [
        "Only do what the user asked. Don't open Facebook Marketplace unless they asked for listings."
    ]
    steered = _steer_listings(instruction)
    if steered != instruction.strip():
        extra.append(
            "Listings search: Facebook Marketplace only. Not Craigslist."
        )
        extra.append(
            "Search Marketplace, open listings, scroll. Don't stop on the homepage if they asked what's for sale."
        )
    if "x_user" in sensitive and "x_pass" in sensitive:
        extra.append(
            "If there's a login form, sign in with username x_user and password x_pass."
        )
    extra.append(
        "If they asked you to send a message, type it, click send, and check it posted."
    )
    extra.append(
        "If you hit a login wall and can't continue, wait in this browser for someone to log in, then keep going."
    )
    if source == "phone":
        extra.append(
            "They're on a phone call. Final result should be a short spoken summary of what you did. "
            "Names and prices only if you searched listings. No URLs, no markdown."
        )
    return steered + "\n\n" + " ".join(extra)


async def _screenshot_jpeg_b64(browser: Browser) -> str | None:
    """Screenshot the page the agent is on."""
    page = await browser.get_current_page()
    if page is None:
        return None
    data = await page.screenshot(format="jpeg", quality=55)
    if not data:
        return None
    if isinstance(data, bytes):
        import base64

        return base64.b64encode(data).decode("ascii")
    text = str(data)
    if text.startswith("data:"):
        return text.split(",", 1)[-1]
    return text


@dataclass
class TaskSession:
    instruction: str
    source: TaskSource = "web"
    task_id: str = field(default_factory=lambda: uuid4().hex)
    status: TaskStatus = "running"
    result: str | None = None
    logs: list[str] = field(default_factory=list)
    latest_frame: str | None = None
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    browser: Browser | None = None
    secrets: list[str] = field(default_factory=list)
    agent: Agent | None = None
    stop_requested: bool = False
    spoken: list[str] = field(default_factory=list)
    spoken_index: int = 0
    last_spoken: str = ""

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self.subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        if queue in self.subscribers:
            self.subscribers.remove(queue)

    def push_spoken(self, text: str) -> None:
        line = _for_speech(text, extra=self.secrets)
        if not line:
            return
        if self.spoken and _similar_speech(self.spoken[-1], line):
            return
        self.spoken.append(line)

    def consume_spoken(self) -> str | None:
        if self.spoken_index >= len(self.spoken):
            return None
        line = self.spoken[-1]
        self.spoken_index = len(self.spoken)
        if self.last_spoken and _similar_speech(self.last_spoken, line):
            return None
        self.last_spoken = line
        return line

    async def emit(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "frame":
            self.latest_frame = message.get("data")
        elif kind == "log" and message.get("text"):
            message["text"] = _redact(str(message["text"]), extra=self.secrets)
            self.logs.append(str(message["text"]))
        for queue in list(self.subscribers):
            await queue.put(message)


class AgentRunner:
    """One running task at a time."""

    def __init__(self) -> None:
        self.tasks: dict[str, TaskSession] = {}
        self.latest_task_id: str | None = None
        self._lock = asyncio.Lock()
        self._active: asyncio.Task | None = None
        self._display = None

    def current_running(self) -> TaskSession | None:
        if not self.latest_task_id:
            return None
        session = self.tasks.get(self.latest_task_id)
        if session and session.status == "running":
            return session
        return None

    async def start_task(
        self,
        instruction: str,
        source: TaskSource = "web",
    ) -> TaskSession:
        instruction = instruction.strip()
        if not instruction:
            raise ValueError("instruction is required")

        if not os.getenv("ANTHROPIC_API_KEY"):
            raise ValueError("ANTHROPIC_API_KEY is not set")

        sensitive = _build_sensitive_data()

        async with self._lock:
            running = self.current_running()
            if running:
                raise RuntimeError(f"A task is already running ({running.task_id})")
            session = TaskSession(instruction=instruction, source=source)
            session.secrets = [v for v in sensitive.values() if v]
            self.tasks[session.task_id] = session
            self.latest_task_id = session.task_id
            self._active = asyncio.create_task(self._run(session, sensitive))
            return session

    async def stop_current(self) -> TaskSession:
        session = self.current_running()
        if session is None:
            raise RuntimeError("No running task")
        session.stop_requested = True
        if session.agent is not None:
            try:
                session.agent.stop()
            except Exception:
                logger.warning("agent.stop() failed", exc_info=True)
        await session.emit({"type": "log", "text": "Stop requested, shutting this run down."})
        return session

    async def reset(self) -> None:
        """Stop the run and clear demo state. Keeps the Chrome login profile."""
        session = self.current_running()
        if session is not None:
            session.stop_requested = True
            if session.agent is not None:
                try:
                    session.agent.stop()
                except Exception:
                    logger.warning("agent.stop() failed during reset", exc_info=True)
            if session.browser is not None:
                try:
                    await session.browser.kill()
                except Exception:
                    try:
                        await session.browser.stop()
                    except Exception:
                        logger.warning("browser close failed during reset", exc_info=True)
        active = self._active
        self.tasks.clear()
        self.latest_task_id = None
        self._active = None
        if active is not None and not active.done():
            active.cancel()
            try:
                await asyncio.wait_for(active, timeout=2)
            except Exception:
                pass

    async def _run(self, session: TaskSession, sensitive: dict[str, str]) -> None:
        if self._display is None:
            self._display = _ensure_virtual_display()

        stop = asyncio.Event()
        browser: Browser | None = None
        capture_task: asyncio.Task | None = None

        try:
            model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
            llm = ChatAnthropic(model=model, temperature=0.0)
            browser = _make_browser()
            session.browser = browser
            await session.emit({"type": "log", "text": f"Starting agent with {model}"})
            chrome = _system_chrome_path()
            await session.emit(
                {
                    "type": "log",
                    "text": (
                        f"Browser: system Chrome ({chrome})"
                        if chrome
                        else "Browser: bundled Chromium (sites like Roblox often block this)"
                    ),
                }
            )
            await session.emit({"type": "log", "text": f"Instruction: {session.instruction}"})
            if sensitive:
                await session.emit(
                    {
                        "type": "log",
                        "text": "Login from .env (hidden). Cookies stay in the local Chrome profile.",
                    }
                )

            async def on_step_end(agent: Agent) -> None:
                await session.emit({"type": "log", "text": _format_step(agent)})
                spoken = _speakable_step(agent)
                if spoken:
                    session.push_spoken(spoken)

            agent_kwargs: dict[str, Any] = {
                "task": _task_text(session.instruction, sensitive, session.source),
                "llm": llm,
                "browser": browser,
                "use_vision": True,
                "extend_system_message": INTERACT_INSTRUCTIONS,
            }
            if sensitive:
                agent_kwargs["sensitive_data"] = sensitive

            agent = Agent(**agent_kwargs)
            session.agent = agent
            capture_task = asyncio.create_task(self._capture_loop(session, stop))
            history = await agent.run(on_step_end=on_step_end, max_steps=MAX_STEPS)

            result = ""
            try:
                result = history.final_result() or ""
            except Exception:
                result = ""
            if not result:
                try:
                    extracted = history.extracted_content()
                    if extracted:
                        result = str(extracted[-1])
                except Exception:
                    result = str(history)

            if session.stop_requested:
                session.status = "done"
                session.result = "Stopped by user."
                await session.emit({"type": "log", "text": "Stopped by user."})
                await session.emit({"type": "done", "result": session.result})
                return

            session.status = "done"
            session.result = result
            await session.emit({"type": "log", "text": f"Done: {result}"})
            await session.emit({"type": "done", "result": result})
        except Exception as exc:
            if session.stop_requested:
                session.status = "done"
                session.result = "Stopped by user."
                await session.emit({"type": "log", "text": "Stopped by user."})
                await session.emit({"type": "done", "result": session.result})
                return
            logger.exception("Task %s failed", session.task_id)
            session.status = "error"
            session.result = str(exc)
            await session.emit({"type": "log", "text": f"Error: {exc}"})
            await session.emit({"type": "done", "result": str(exc)})
        finally:
            stop.set()
            if capture_task is not None:
                try:
                    await asyncio.wait_for(capture_task, timeout=2)
                except Exception:
                    capture_task.cancel()
            if browser is not None:
                try:
                    await browser.kill()
                except Exception:
                    try:
                        await browser.stop()
                    except Exception:
                        logger.warning("Failed to close browser for %s", session.task_id)
            session.browser = None
            session.agent = None

    async def _capture_loop(self, session: TaskSession, stop: asyncio.Event) -> None:
        while not stop.is_set():
            browser = session.browser
            if browser is not None:
                try:
                    frame = await _screenshot_jpeg_b64(browser)
                    if frame:
                        await session.emit({"type": "frame", "data": frame})
                except Exception:
                    logger.debug("Screenshot tick failed", exc_info=True)
            try:
                await asyncio.wait_for(stop.wait(), timeout=FRAME_INTERVAL_S)
            except TimeoutError:
                continue
