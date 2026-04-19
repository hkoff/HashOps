# src/web_ui/sse.py — Server-Sent Events (SSE) & Logging Infrastructure
#
# This module is decoupled from app.py to prevent circular imports. 
# It manages browser subscribers, real-time broadcasts, and the SSE log bridge.

import json
import logging
import queue
import threading
from flask import Response
from src.services.logger_setup import logger

# ─────────────────────────────────────────────────────────────────
# SSE STATE
# ─────────────────────────────────────────────────────────────────
# Each connected browser tab gets its own Queue. When the backend
# broadcasts an event, it is pushed to every active queue.
_subscribers: list[queue.Queue] = []
_subscribers_lock = threading.Lock()


def _broadcast(event: dict) -> None:
    """
    Push a JSON event to all SSE-connected browser tabs.
    Thread-safe. Automatically removes subscribers whose queue is full (i.e. the browser tab has disconnected or is too slow to consume).
    """
    with _subscribers_lock:
        dead = []
        for q in _subscribers:
            try:
                q.put_nowait(event)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _subscribers.remove(q)


class _SSELogHandler(logging.Handler):
    """
    Forwards Python log records to the browser log terminal via SSE.
    Attached to the central logger so that every log message from any module (actions, core, services) is automatically visible in the UI.
    """
    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.format(record)
            _broadcast({
                "type":    "log",
                "level":   record.levelname,
                "message": record.getMessage(),
                "time":    record.asctime if hasattr(record, "asctime") else "",
            })
        except Exception:
            pass

_sse_handler = _SSELogHandler()
_sse_handler.setLevel(logging.DEBUG)
logger.addHandler(_sse_handler)


def get_sse_response():
    """
    Creates and returns a Flask Response object configured for SSE.
    Handles subscriber queue management and the real-time event generator.
    """
    q: queue.Queue = queue.Queue(maxsize=500)
    with _subscribers_lock:
        _subscribers.append(q)

    def generate():
        try:
            yield 'data: {"type": "connected"}\n\n'
            while True:
                try:
                    event = q.get(timeout=30)
                    yield f"data: {json.dumps(event)}\n\n"
                except queue.Empty:
                    yield 'data: {"type": "ping"}\n\n'
        except GeneratorExit:
            pass
        finally:
            with _subscribers_lock:
                if q in _subscribers:
                    _subscribers.remove(q)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
