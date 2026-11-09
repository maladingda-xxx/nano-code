"""Record file edits, list checkpoints and restore them without Git."""

import hashlib
import os
import sqlite3
import stat
import tempfile
from contextlib import closing
from datetime import datetime
from pathlib import Path

from edit_context import current_edit_context
from exceptions import ToolExecutionError, ToolInputError, ToolSecurityError
from session_store import _get_db_path
from workspace import workspace_path


def _ensure_journal(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS file_edits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL,
            before BLOB,
            after_hash BLOB NOT NULL
        )
    """)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(file_edits)")}
    for name, kind in (
        ("session_id", "TEXT"), ("turn_id", "TEXT"),
        ("tool_call_id", "TEXT"), ("created_at", "TEXT"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE file_edits ADD COLUMN {name} {kind}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_file_edits_turn ON file_edits(turn_id)")


def read_bytes(path: Path) -> bytes | None:
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            raise OSError("只能操作普通文件。")
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _replace(path: Path, before: bytes | None, after: bytes | None) -> None:
    if after is None:
        if read_bytes(path) != before:
            raise OSError("文件内容已变化，请重新读取后再操作。")
        path.unlink()
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temp_path = Path(stream.name)
        try:
            stream.write(after)
            stream.close()
            if read_bytes(path) != before:
                raise OSError("文件内容已变化，请重新读取后再操作。")
            if before is not None:
                temp_path.chmod(stat.S_IMODE(path.stat().st_mode))
            temp_path.replace(path)
        finally:
            temp_path.unlink(missing_ok=True)


def record_edit(
    root: Path, path: Path, before: bytes | None, after: bytes, tool: str,
) -> None:
    if before == after:
        return
    context = current_edit_context() or {}
    try:
        with closing(sqlite3.connect(_get_db_path())) as conn:
            with conn:
                _ensure_journal(conn)
                row = conn.execute(
                    """INSERT INTO file_edits
                       (path, before, after_hash, session_id, turn_id, tool_call_id, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        str(path.relative_to(root)), before, hashlib.sha256(after).digest(),
                        context.get("session_id"), context.get("turn_id"),
                        context.get("tool_call_id"), datetime.now().isoformat(timespec="seconds"),
                    ),
                )
            # Commit the snapshot before changing the file so interruption can be recovered.
            try:
                with conn:
                    conn.execute("BEGIN IMMEDIATE")
                    if conn.execute(
                        "SELECT 1 FROM file_edits WHERE id = ?", (row.lastrowid,)
                    ).fetchone() is None:
                        raise OSError("这次修改已被撤销。")
                    _replace(path, before, after)
            except OSError:
                with conn:
                    conn.execute("DELETE FROM file_edits WHERE id = ?", (row.lastrowid,))
                raise
    except (OSError, sqlite3.Error) as error:
        raise ToolExecutionError(f"无法保存文件修改: {error}", tool_name=tool) from error


def undo_last_edit(root: Path, session_id: str | None = None) -> str:
    if not Path(_get_db_path()).is_file():
        return "没有可撤销的文件修改。"
    try:
        with closing(sqlite3.connect(_get_db_path())) as conn, conn:
            _ensure_journal(conn)
            conn.execute("BEGIN IMMEDIATE")
            legacy = False
            if session_id is None:
                row = conn.execute(
                    "SELECT id, path, before, after_hash FROM file_edits ORDER BY id DESC LIMIT 1"
                ).fetchone()
            else:
                row = conn.execute(
                    """SELECT id, path, before, after_hash FROM file_edits
                       WHERE session_id = ? ORDER BY id DESC LIMIT 1""",
                    (session_id,),
                ).fetchone()
                if row is None:
                    row = conn.execute(
                        """SELECT id, path, before, after_hash FROM file_edits
                           WHERE session_id IS NULL ORDER BY id DESC LIMIT 1"""
                    ).fetchone()
                    legacy = row is not None
            if row is None:
                if session_id is None:
                    return "没有可撤销的文件修改。"
                return "当前会话没有可撤销的文件修改。"
            edit_id, name, before, after_hash = row
            path = workspace_path(root, name, "undo")
            current = read_bytes(path)
            label = "旧版未关联会话的" if legacy else ""
            if current == before:
                result = f"{name} 已是修改前的内容。"
            elif current is not None and hashlib.sha256(current).digest() == after_hash:
                _replace(path, current, before)
                result = f"已撤销{label}对 {name} 的最后一次文件修改。"
            else:
                return f"{name} 在修改后发生了变化，已保留当前内容。"
            conn.execute("DELETE FROM file_edits WHERE id = ?", (edit_id,))
            return result
    except (OSError, sqlite3.Error) as error:
        raise ToolExecutionError(f"无法撤销文件修改: {error}", tool_name="undo") from error


def _checkpoint_paths(conn: sqlite3.Connection, turn_id: str) -> list[dict]:
    rows = conn.execute(
        """SELECT id, path, before, after_hash FROM file_edits
           WHERE turn_id = ? ORDER BY id""",
        (turn_id,),
    ).fetchall()
    entries: dict[str, dict] = {}
    order = []
    for edit_id, name, before, after_hash in rows:
        entry = entries.get(name)
        if entry is None:
            entry = {
                "path": name, "before": before, "after_hash": after_hash,
                "edits": 0, "first_id": edit_id, "last_id": edit_id,
            }
            entries[name] = entry
            order.append(name)
        entry["edits"] += 1
        entry["last_id"] = edit_id
        entry["after_hash"] = after_hash
    return [entries[name] for name in order]


def list_checkpoints(limit: int = 20) -> list[dict]:
    """List recent turns that changed workspace files."""
    if not Path(_get_db_path()).is_file():
        return []
    try:
        with closing(sqlite3.connect(_get_db_path())) as conn, conn:
            _ensure_journal(conn)
            rows = conn.execute(
                """SELECT id, turn_id, session_id, path, created_at FROM file_edits
                   ORDER BY id DESC"""
            ).fetchall()
    except sqlite3.Error as error:
        raise ToolExecutionError(f"无法读取文件检查点: {error}", tool_name="checkpoints") from error

    checkpoints: dict[tuple[str, str], dict] = {}
    for _, turn_id, session_id, name, created_at in rows:
        key = (turn_id or "", session_id or "")
        entry = checkpoints.get(key)
        if entry is None:
            if len(checkpoints) >= limit:
                continue
            entry = {
                "turn_id": turn_id or "", "session_id": session_id or "",
                "created_at": created_at, "edits": 0, "paths": [],
            }
            checkpoints[key] = entry
        entry["edits"] += 1
        if name not in entry["paths"]:
            entry["paths"].append(name)
        if entry["created_at"] is None:
            entry["created_at"] = created_at
    for entry in checkpoints.values():
        entry["files"] = len(entry["paths"])
    return list(checkpoints.values())


def resolve_turn_id(prefix: str) -> str:
    """Resolve an exact turn id or a unique prefix from the edit journal."""
    prefix = (prefix or "").strip()
    if not prefix:
        raise ToolInputError("请提供检查点的轮次 ID。", tool_name="checkpoints")
    if not Path(_get_db_path()).is_file():
        raise ToolInputError("没有可用的文件检查点。", tool_name="checkpoints")
    try:
        with closing(sqlite3.connect(_get_db_path())) as conn, conn:
            _ensure_journal(conn)
            rows = conn.execute(
                """SELECT DISTINCT turn_id FROM file_edits
                   WHERE turn_id IS NOT NULL AND turn_id != ''"""
            ).fetchall()
    except sqlite3.Error as error:
        raise ToolExecutionError(f"无法读取文件检查点: {error}", tool_name="checkpoints") from error
    matches = [turn for (turn,) in rows if turn == prefix or turn.startswith(prefix)]
    if not matches:
        raise ToolInputError("找不到包含文件修改的轮次。", tool_name="checkpoints")
    if len(matches) > 1:
        raise ToolInputError("轮次 ID 前缀不唯一，请提供更多字符。", tool_name="checkpoints")
    return matches[0]


def _preview_entry(root: Path, entry: dict) -> dict:
    name = entry["path"]
    try:
        path = workspace_path(root, name, "checkpoints")
        current = read_bytes(path)
    except (OSError, ToolInputError, ToolSecurityError) as error:
        return {**entry, "status": "conflict", "detail": str(error)}
    if current == entry["before"]:
        status, detail = "already_restored", "已是修改前内容"
    elif current is not None and hashlib.sha256(current).digest() == entry["after_hash"]:
        status, detail = "restorable", "可恢复到本轮开始前"
    elif current is None:
        status, detail = "missing", "文件不存在，无法确认当前状态"
    else:
        status, detail = "conflict", "文件在本轮修改后发生变化"
    return {**entry, "status": status, "detail": detail}


def preview_checkpoint(root: Path, turn_id: str) -> list[dict]:
    """Describe which files in a turn can be restored and which conflict."""
    if not Path(_get_db_path()).is_file():
        return []
    try:
        with closing(sqlite3.connect(_get_db_path())) as conn, conn:
            _ensure_journal(conn)
            entries = _checkpoint_paths(conn, turn_id)
    except sqlite3.Error as error:
        raise ToolExecutionError(f"无法读取文件检查点: {error}", tool_name="checkpoints") from error
    preview = [_preview_entry(root, entry) for entry in entries]
    return [
        {
            "path": entry["path"], "status": entry["status"],
            "detail": entry["detail"], "edits": entry["edits"],
        }
        for entry in preview
    ]


def _delete_edits(turn_id: str, name: str) -> None:
    try:
        with closing(sqlite3.connect(_get_db_path())) as conn, conn:
            conn.execute(
                "DELETE FROM file_edits WHERE turn_id = ? AND path = ?", (turn_id, name),
            )
    except sqlite3.Error as error:
        raise ToolExecutionError(f"无法更新文件检查点: {error}", tool_name="restore") from error


def restore_checkpoint(root: Path, turn_id: str) -> dict:
    """Restore every file in a turn that still matches its recorded result."""
    result = {"restored": [], "already": [], "conflicts": [], "failed": []}
    if not Path(_get_db_path()).is_file():
        return result
    try:
        with closing(sqlite3.connect(_get_db_path())) as conn, conn:
            _ensure_journal(conn)
            entries = _checkpoint_paths(conn, turn_id)
    except sqlite3.Error as error:
        raise ToolExecutionError(f"无法读取文件检查点: {error}", tool_name="restore") from error

    for entry in entries:
        name = entry["path"]
        try:
            path = workspace_path(root, name, "restore")
            current = read_bytes(path)
        except (OSError, ToolInputError, ToolSecurityError) as error:
            result["conflicts"].append({"path": name, "detail": str(error)})
            continue
        if current == entry["before"]:
            try:
                _delete_edits(turn_id, name)
            except ToolExecutionError as error:
                result["failed"].append({"path": name, "detail": str(error)})
            else:
                result["already"].append(name)
            continue
        if current is None:
            result["conflicts"].append({"path": name, "detail": "文件不存在"})
            continue
        if hashlib.sha256(current).digest() != entry["after_hash"]:
            result["conflicts"].append({
                "path": name, "detail": "文件在本轮修改后发生变化，已保留当前内容",
            })
            continue
        try:
            _replace(path, current, entry["before"])
            _delete_edits(turn_id, name)
        except (OSError, ToolExecutionError) as error:
            result["failed"].append({"path": name, "detail": str(error)})
            continue
        result["restored"].append(name)
    return result
