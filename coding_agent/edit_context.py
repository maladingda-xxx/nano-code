"""Carry the current session, turn and tool call into file edits."""

from contextlib import contextmanager
from contextvars import ContextVar

_current: ContextVar[dict | None] = ContextVar("edit_context", default=None)


def current_edit_context() -> dict | None:
    return _current.get()


@contextmanager
def edit_scope(session_id=None, turn_id=None, tool_call_id=None):
    token = _current.set({
        "session_id": session_id,
        "turn_id": turn_id,
        "tool_call_id": tool_call_id,
    })
    try:
        yield
    finally:
        _current.reset(token)
