"""Маленькая обвязка, которая превращает функцию-обработчик в сервис агента для платформы.

Автор агента пишет одну функцию `handler(run)` и вызывает `serve(handler, name=...)`. Обвязка даёт:
  POST /agui     — запуск: на входе запрос протокола AG-UI, на выходе поток событий (SSE);
  GET  /healthz  — проверка, что сервис жив.
Вход по токену приложения: заголовок `Authorization: Bearer <токен>`, список токенов — в UAI_AGENT_TOKENS.

Внутри обработчика:
  run.task            — текст последнего сообщения пользователя;
  run.state           — состояние, которое интерфейс хранит между сообщениями (прошлый результат);
  run.params          — параметры из адреса запроса (?mode=…&model=…);
  run.text("…")       — сообщение агента в чат;
  run.tool_start(id, name, args) / run.tool_result(id, result) — шаг работы: начат и закончен;
  run.set_state({...}) — промежуточный или итоговый результат для интерфейса;
  run.cancelled       — пользователь закрыл соединение, работу можно прекращать.

К модели агент ходит только через шлюз маскирования платформы: `gateway_completion()` возвращает
функцию вызова модели, которая сама подставляет адрес шлюза и заголовки маскирования.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import threading
import uuid
from typing import Any, Callable

from ag_ui.core import (
    EventType, RunErrorEvent, RunFinishedEvent, RunStartedEvent, StateSnapshotEvent,
    TextMessageContentEvent, TextMessageEndEvent, TextMessageStartEvent,
    ToolCallArgsEvent, ToolCallEndEvent, ToolCallResultEvent, ToolCallStartEvent,
)
from ag_ui.encoder import EventEncoder
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

MAX_BODY_BYTES = 2_000_000


class Cancelled(Exception):
    """Клиент отключился: обработчик может прекратить работу."""


def _tokens() -> list[str]:
    return [t.strip() for t in os.environ.get("UAI_AGENT_TOKENS", "").split(",") if t.strip()]


def _authorized(request: Request) -> bool:
    given = request.headers.get("authorization", "")
    if not given.lower().startswith("bearer "):
        return False
    token = given[7:].strip()
    return any(hmac.compare_digest(token.encode(), t.encode()) for t in _tokens())


def _last_user_text(messages: list) -> str:
    for m in reversed(messages or []):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return "\n".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
    return ""


class Run:
    """Один запуск агента: что пришло и чем ответить."""

    def __init__(self, body: dict, params: dict, emit: Callable[[Any], None]):
        self.thread_id = str(body.get("threadId") or uuid.uuid4())
        self.run_id = str(body.get("runId") or uuid.uuid4())
        self.task = _last_user_text(body.get("messages") or [])
        self.state = body.get("state") if isinstance(body.get("state"), dict) else {}
        self.params = params
        self.cancelled = False
        self._emit = emit
        self._message_id = f"msg-{uuid.uuid4().hex[:12]}"

    def text(self, text: str) -> None:
        if not text:
            return
        mid = f"msg-{uuid.uuid4().hex[:12]}"
        self._message_id = mid
        self._emit(TextMessageStartEvent(type=EventType.TEXT_MESSAGE_START, message_id=mid, role="assistant"))
        self._emit(TextMessageContentEvent(type=EventType.TEXT_MESSAGE_CONTENT, message_id=mid, delta=text))
        self._emit(TextMessageEndEvent(type=EventType.TEXT_MESSAGE_END, message_id=mid))

    def tool_start(self, call_id: str, name: str, args: dict) -> None:
        self._emit(ToolCallStartEvent(type=EventType.TOOL_CALL_START, tool_call_id=call_id, tool_call_name=name))
        self._emit(ToolCallArgsEvent(type=EventType.TOOL_CALL_ARGS, tool_call_id=call_id,
                                     delta=json.dumps(args, ensure_ascii=False)))
        self._emit(ToolCallEndEvent(type=EventType.TOOL_CALL_END, tool_call_id=call_id))

    def tool_result(self, call_id: str, result: Any) -> None:
        content = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        self._emit(ToolCallResultEvent(type=EventType.TOOL_CALL_RESULT, message_id=f"res-{uuid.uuid4().hex[:12]}",
                                       tool_call_id=call_id, content=content, role="tool"))

    def set_state(self, state: dict) -> None:
        # Копия: событие уходит в поток позже, а обработчик продолжает менять свой словарь.
        self._emit(StateSnapshotEvent(type=EventType.STATE_SNAPSHOT, snapshot=json.loads(json.dumps(state))))

    def check_cancelled(self) -> None:
        if self.cancelled:
            raise Cancelled()


def gateway_completion() -> Callable:
    """Вызов модели через шлюз платформы: адрес и заголовки маскирования подставляются здесь."""
    import litellm

    litellm.suppress_debug_info = True

    def completion(*, mask_scope: str | None = None, **kwargs):
        headers = dict(kwargs.pop("extra_headers", None) or {})
        headers.setdefault("X-Mask-Key", os.environ["UAI_MASK_KEY"])
        headers.setdefault("X-Mask-Scope", mask_scope or os.environ.get("UAI_MASK_SCOPE", "default"))
        kwargs["extra_headers"] = headers
        kwargs.setdefault("api_base", os.environ["UAI_MODEL_BASE_URL"])
        return litellm.completion(**kwargs)

    return completion


def create_app(handler: Callable[[Run], None], *, name: str) -> Starlette:
    encoder = EventEncoder()

    async def healthz(_: Request):
        return JSONResponse({"ok": True, "agent": name})

    async def agui(request: Request):
        if not _tokens():
            return JSONResponse({"error": "UAI_AGENT_TOKENS is not set"}, status_code=503)
        if not _authorized(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            return JSONResponse({"error": "Request is too large"}, status_code=413)
        try:
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return JSONResponse({"error": "Body must be a JSON object"}, status_code=400)

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        done = object()

        def emit(event) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, event)

        run = Run(body, dict(request.query_params), emit)

        def work() -> None:
            emit(RunStartedEvent(type=EventType.RUN_STARTED, thread_id=run.thread_id, run_id=run.run_id))
            try:
                handler(run)
                emit(RunFinishedEvent(type=EventType.RUN_FINISHED, thread_id=run.thread_id, run_id=run.run_id))
            except Cancelled:
                pass
            except Exception as e:  # ошибка обработчика уходит в интерфейс как событие, а не обрывом потока
                print(f"[{name}] run {run.run_id} failed: {type(e).__name__}: {e}", flush=True)
                emit(RunErrorEvent(type=EventType.RUN_ERROR, message=f"{type(e).__name__}: {e}"[:500]))
            finally:
                emit(done)

        threading.Thread(target=work, name=f"run-{run.run_id[:8]}", daemon=True).start()

        async def stream():
            try:
                while True:
                    event = await queue.get()
                    if event is done:
                        break
                    yield encoder.encode(event)
            finally:
                run.cancelled = True  # клиент ушёл или поток закончен

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    return Starlette(routes=[Route("/healthz", healthz), Route("/agui", agui, methods=["POST"])])


def serve(handler: Callable[[Run], None], *, name: str) -> None:
    import uvicorn

    uvicorn.run(create_app(handler, name=name), host=os.environ.get("HOST", "0.0.0.0"),
                port=int(os.environ.get("PORT", "8080")), log_level="warning")
