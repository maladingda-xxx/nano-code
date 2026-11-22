"""Session-scoped file read state.

This is the small Python counterpart of Claude Code's ``FileStateCache``:

- ``read_file`` records the path, file version (mtime/size), and line range.
- ``edit_file`` requires that the file was read in this session and has not
  changed since that read.
- ``write_file`` requires a *full* read before overwriting or appending to an
  existing file. Creating a new file does not require a read.
- Repeating the exact same read in the same turn returns ``UnchangedOutput``
  instead of sending the same content twice.

The cache is in memory and per session. It is deliberately not persisted:
after a restart, or after context compaction, the model should read the file
again before editing it.
"""

from __future__ import annotations

import os
from collections import OrderedDict
from dataclasses import dataclass, replace
from pathlib import Path

from edit_context import current_edit_context
from workspace import workspace_path


class UnchangedOutput(str):
    """A tool result that says the file is unchanged.

    It is a ``str`` subclass so existing callers that treat tool results as
    strings keep working; the dispatcher can use ``isinstance`` to mark the
    structured status as ``unchanged``.
    """


@dataclass(frozen=True)
class FileState:
    mtime_ns: int
    size: int
    start_line: int
    end_line: int
    full_read: bool
    turn_id: str | None
    round_id: int | None
    tool_call_id: str | None = None
    full_read_call_id: str | None = None


class FileStateCache:
    def __init__(self, max_entries: int = 256):
        self.max_entries = max_entries
        self._items: OrderedDict[str, FileState] = OrderedDict()

    def get(self, path: Path | str) -> FileState | None:
        key = _key(path)
        state = self._items.get(key)
        if state is not None:
            self._items.move_to_end(key)
        return state

    def put(self, path: Path | str, state: FileState) -> None:
        key = _key(path)
        self._items[key] = state
        self._items.move_to_end(key)
        while len(self._items) > self.max_entries:
            self._items.popitem(last=False)

    def remove(self, path: Path | str) -> None:
        self._items.pop(_key(path), None)

    def clear(self) -> None:
        self._items.clear()


_default = FileStateCache()
_sessions: dict[str, FileStateCache] = {}


def _key(path: Path | str) -> str:
    return os.path.normcase(str(Path(path).resolve()))


def _cache() -> FileStateCache:
    context = current_edit_context() or {}
    session_id = context.get("session_id")
    if not session_id:
        return _default
    cache = _sessions.get(session_id)
    if cache is None:
        cache = FileStateCache()
        _sessions[session_id] = cache
    return cache


def _turn_id() -> str | None:
    return (current_edit_context() or {}).get("turn_id")


def _round_id() -> int | None:
    return (current_edit_context() or {}).get("round")


def _tool_call_id() -> str | None:
    return (current_edit_context() or {}).get("tool_call_id")


def lookup(path: Path | str) -> FileState | None:
    return _cache().get(path)


def record_read(
    path: Path | str,
    *,
    start_line: int,
    end_line: int,
    full_read: bool,
) -> None:
    """Remember a successful read after the file has been read."""
    target = Path(path)
    try:
        stat = target.stat()
    except OSError:
        return

    previous = _cache().get(target)
    # A full read stays valid while the file version is unchanged, even if the
    # model later reads a smaller page. Overwriting still needs a full view.
    previous_full = (
        previous is not None and previous.full_read
        and previous.mtime_ns == stat.st_mtime_ns and previous.size == stat.st_size
    )
    call_id = _tool_call_id()
    full_read_call_id = call_id if full_read else None
    if not full_read and previous_full:
        full_read_call_id = previous.full_read_call_id
    _cache().put(
        target,
        FileState(
            mtime_ns=stat.st_mtime_ns,
            size=stat.st_size,
            start_line=start_line,
            end_line=end_line,
            full_read=full_read or previous_full,
            turn_id=_turn_id(),
            round_id=_round_id(),
            tool_call_id=call_id,
            full_read_call_id=full_read_call_id,
        ),
    )


def record_written(path: Path | str) -> None:
    """A successful edit/write means the model has the full new content."""
    target = Path(path)
    try:
        stat = target.stat()
    except OSError:
        return
    _cache().put(
        target,
        FileState(
            mtime_ns=stat.st_mtime_ns,
            size=stat.st_size,
            start_line=1,
            end_line=1,
            full_read=True,
            turn_id=_turn_id(),
            round_id=_round_id(),
            tool_call_id=_tool_call_id(),
            full_read_call_id=_tool_call_id(),
        ),
    )


def is_unchanged(path: Path | str, *, start_line: int, end_line: int) -> bool:
    """Return True only for the same range in a later round of the same turn.

    Reads issued in the same assistant message are not deduplicated: the model
    has not seen the first result yet. A later model round has the earlier
    tool result in its request, so the duplicate can be replaced by a stub.
    """
    turn_id = _turn_id()
    round_id = _round_id()
    if not turn_id or round_id is None:
        # Direct/programmatic calls have no turn; do not hide content from them.
        return False

    state = lookup(path)
    if (
        state is None
        or state.turn_id != turn_id
        or state.round_id is None
        or state.round_id == round_id
        or state.start_line != start_line
        or state.end_line != end_line
    ):
        return False

    try:
        stat = Path(path).stat()
    except OSError:
        return False
    return state.mtime_ns == stat.st_mtime_ns and state.size == stat.st_size


def invalidate(path: Path | str, *, tool_call_id: str | None = None, root: Path | None = None) -> None:
    """Expire read state backed by a pruned tool result.

    With a workspace root, resolve relative tool arguments there. Callers
    without a root can match a model-facing path by its suffix.
    """
    raw = os.path.normcase(str(path)).replace("\\", "/").lstrip("./")
    if not raw:
        return
    try:
        resolved = _key(root / path if root is not None else path).replace("\\", "/")
    except (OSError, ValueError, TypeError, RuntimeError):
        return
    for cache in (_default, *_sessions.values()):
        for key, state in list(cache._items.items()):
            normalized = key.replace("\\", "/")
            if normalized != resolved and (root is not None or not normalized.endswith("/" + raw)):
                continue
            if tool_call_id is None or state.tool_call_id in {None, tool_call_id}:
                cache._items.pop(key, None)
            elif state.full_read and state.full_read_call_id == tool_call_id:
                cache._items[key] = replace(state, full_read=False, full_read_call_id=None)


def reset_session(session_id: str | None = None) -> None:
    if session_id is None:
        _sessions.clear()
        _default.clear()
    else:
        _sessions.pop(session_id, None)


def reset_all() -> None:
    _sessions.clear()
    _default.clear()


def check_edit_access(name: str, arguments: dict, root: Path) -> str | None:
    """Return an actionable error when a file has not been read or is stale.

    The dispatcher calls this for product entry points. Direct calls to
    ``tools.edit_file`` (a low-level Python integration that bypasses the
    dispatcher) are not gated; the tool still records state after success.
    """
    file_path = arguments.get("file_path")
    if not isinstance(file_path, str) or not file_path.strip():
        return None

    if name == "edit_file":
        if not isinstance(arguments.get("old_text"), str) or not arguments["old_text"]:
            return None
        full_required = False
        action = "编辑"
    elif name == "write_file":
        mode = arguments.get("mode", "overwrite")
        if mode not in {"overwrite", "append"} or not isinstance(arguments.get("content"), str):
            return None
        full_required = True
        action = "覆盖或追加"
    else:
        return None

    path = workspace_path(root, file_path, name)

    if not path.exists():
        # New files need no read; a missing target for edit should be reported
        # by the tool itself with its own error.
        return None
    if not path.is_file():
        return None

    state = lookup(path)
    if state is None:
        return f"尚未读取 {file_path}。请先用 read_file 读取该文件，再{action}。"

    if full_required and not state.full_read:
        return f"{action}已有文件前需要完整读取 {file_path}；当前只读取了部分内容。请先完整 read_file。"

    try:
        stat = path.stat()
    except OSError:
        return None
    if state.mtime_ns != stat.st_mtime_ns or state.size != stat.st_size:
        return f"{file_path} 在读取后已发生变化。请重新 read_file 后再修改。"

    return None
