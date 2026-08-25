"""HTTP API for Dispatch: run browser tasks, stream frames, handle Twilio calls."""

from __future__ import annotations

import logging
import os
import re
import xml.sax.saxutils
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from agent_runner import AgentRunner

load_dotenv(Path(__file__).resolve().parent / ".env")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("dispatch")

app = FastAPI(title="Dispatch")
runner = AgentRunner()

_origins = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1|\d{1,3}(?:\.\d{1,3}){3}|[a-z0-9-]+\.trycloudflare\.com)(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class TaskRequest(BaseModel):
    instruction: str = Field(min_length=1)


_GREETING_RE = re.compile(
    r"^(hi+|hey+|hello+|yo|sup|what'?s up|how are you|good (morning|afternoon|evening)|"
    r"yeah|yes|ok|okay|um+|uh+|hmm+|thanks|thank you)[\s.!]*$",
    re.I,
)


def _is_real_instruction(speech: str) -> bool:
    text = " ".join(speech.split())
    if not text:
        return False
    if _GREETING_RE.match(text):
        return False
    words = re.findall(r"[a-zA-Z0-9']+", text)
    if len(words) < 3:
        return False
    return True


def _gather(request: Request, prompt: str) -> Response:
    collect = f"{_public_base(request)}/voice/collect"
    return _twiml(
        f"""
  <Gather input="speech" action="{collect}" method="POST" speechTimeout="auto" timeout="10">
    <Say>{prompt}</Say>
  </Gather>
  <Say>Still here. Call back if you have a task.</Say>
"""
    )


def _public_base(request: Request) -> str:
    env = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    if env:
        return env
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    return f"{proto}://{host}"


def _twiml(body: str) -> Response:
    return Response(
        content=f'<?xml version="1.0" encoding="UTF-8"?>\n<Response>{body}</Response>',
        media_type="application/xml",
    )


def _say_text(text: str, limit: int = 1200) -> str:
    cleaned = " ".join((text or "Done. Nothing to report.").split())
    return xml.sax.saxutils.escape(cleaned[:limit])


def _task_payload(session) -> dict:
    return {
        "task_id": session.task_id,
        "instruction": session.instruction,
        "status": session.status,
        "source": session.source,
        "result": session.result,
    }


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.post("/tasks")
async def create_task(body: TaskRequest):
    try:
        session = await runner.start_task(
            body.instruction,
            source="web",
        )
    except RuntimeError as exc:
        running = runner.current_running()
        return JSONResponse(
            status_code=409,
            content={
                "detail": str(exc),
                "task_id": running.task_id if running else None,
            },
        )
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc)})
    return {"task_id": session.task_id}


@app.post("/tasks/stop")
async def stop_task():
    try:
        session = await runner.stop_current()
    except RuntimeError as exc:
        return JSONResponse(status_code=409, content={"detail": str(exc)})
    return {"task_id": session.task_id, "status": "stopping"}


@app.post("/tasks/reset")
async def reset_demo():
    await runner.reset()
    return {"ok": True, "latest_task_id": None}


@app.get("/tasks")
async def list_tasks():
    latest = runner.tasks.get(runner.latest_task_id) if runner.latest_task_id else None
    return {
        "latest_task_id": runner.latest_task_id,
        "current": _task_payload(latest) if latest else None,
        "tasks": [_task_payload(s) for s in runner.tasks.values()],
    }


@app.websocket("/ws/{task_id}")
async def task_ws(websocket: WebSocket, task_id: str):
    session = runner.tasks.get(task_id)
    if session is None:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    queue = session.subscribe()
    try:
        for text in session.logs:
            await websocket.send_json({"type": "log", "text": text})
        if session.latest_frame:
            await websocket.send_json({"type": "frame", "data": session.latest_frame})
        if session.status in ("done", "error"):
            await websocket.send_json({"type": "done", "result": session.result or ""})
            return

        while True:
            message = await queue.get()
            await websocket.send_json(message)
            if message.get("type") == "done":
                break
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected for %s", task_id)
    finally:
        session.unsubscribe(queue)


@app.post("/voice")
async def voice_incoming(request: Request):
    """Inbound Twilio call. Ask for a spoken task."""
    return _gather(
        request,
        "Hey. What should I do in the browser?",
    )


@app.post("/voice/collect")
async def voice_collect(request: Request):
    """Twilio posts SpeechResult. Start the same agent as POST /tasks."""
    form = await request.form()
    speech = str(form.get("SpeechResult") or "").strip()
    logger.info("Twilio speech: %s", speech)

    if not _is_real_instruction(speech):
        return _gather(
            request,
            "Didn't catch a task. Say the site and what to do, like search Facebook Marketplace for a couch.",
        )

    try:
        session = await runner.start_task(speech, source="phone")
    except RuntimeError:
        return _twiml(
            "<Say>Already on a task. Try in a bit.</Say><Hangup/>"
        )
    except ValueError:
        return _twiml("<Say>Agent isn't set up. Check the server logs.</Say><Hangup/>")

    wait = f"{_public_base(request)}/voice/wait/{session.task_id}"
    return _twiml(
        f"""
  <Say>Got it. I'll talk through what I'm doing.</Say>
  <Pause length="10"/>
  <Redirect method="POST">{wait}</Redirect>
"""
    )


@app.api_route("/voice/wait/{task_id}", methods=["GET", "POST"])
async def voice_wait(task_id: str, request: Request):
    """Stay on the line until the agent finishes, then read the result."""
    session = runner.tasks.get(task_id)
    if session is None:
        return _twiml("<Say>Lost that one. Bye.</Say><Hangup/>")

    if session.status == "running":
        wait = f"{_public_base(request)}/voice/wait/{task_id}"
        line = session.consume_spoken()
        if line:
            return _twiml(
                f"""
  <Say>{_say_text(line, limit=280)}</Say>
  <Pause length="10"/>
  <Redirect method="POST">{wait}</Redirect>
"""
            )
        return _twiml(
            f"""
  <Pause length="10"/>
  <Redirect method="POST">{wait}</Redirect>
"""
        )

    spoken = _say_text(session.result or "")
    return _twiml(f"<Say>{spoken}</Say><Hangup/>")
