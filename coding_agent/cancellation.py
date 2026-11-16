"""Cooperative cancellation shared by the CLI, SDK and process runner."""

from contextlib import contextmanager
from contextvars import ContextVar

_cancel = ContextVar("nano_cancel", default=None)


def check_cancelled():
    event = _cancel.get()
    if event is not None and event.is_set():
        raise KeyboardInterrupt


@contextmanager
def cancellation_scope(event):
    token = _cancel.set(event)
    try:
        yield
    finally:
        _cancel.reset(token)
