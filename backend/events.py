"""In-process event ring buffer feeding the SSE stream.

Producers run in worker threads (pipeline, triage worker); the SSE endpoint
drains by sequence number — no cross-thread asyncio primitives needed.
"""
from __future__ import annotations

import itertools
import threading
from collections import deque

_lock = threading.Lock()
_buffer: deque[dict] = deque(maxlen=500)
_seq = itertools.count(1)


def publish(event_type: str, data: dict) -> None:
    with _lock:
        _buffer.append({"seq": next(_seq), "type": event_type, "data": data})


def since(seq: int) -> list[dict]:
    with _lock:
        return [e for e in _buffer if e["seq"] > seq]
