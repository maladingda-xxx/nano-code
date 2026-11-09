"""Record file edits and undo them without restoring an entire Git worktree."""

import hashlib
import os
import sqlite3
import stat
import tempfile
from contextlib import closing
from pathlib import Path

from exceptions import ToolExecutionError
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
    try:
        with closing(sqlite3.connect(_get_db_path())) as conn:
            with conn:
                _ensure_journal(conn)
                row = conn.execute(
                    "INSERT INTO file_edits(path, before, after_hash) VALUES (?, ?, ?)",
                    (str(path.relative_to(root)), before, hashlib.sha256(after).digest()),
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


def undo_last_edit(root: Path) -> str:
    if not Path(_get_db_path()).is_file():
        return "没有可撤销的文件修改。"
    try:
        with closing(sqlite3.connect(_get_db_path())) as conn, conn:
            _ensure_journal(conn)
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT id, path, before, after_hash FROM file_edits ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return "没有可撤销的文件修改。"
            edit_id, name, before, after_hash = row
            path = workspace_path(root, name, "undo")
            current = read_bytes(path)
            if current == before:
                result = f"{name} 已是修改前的内容。"
            elif current is not None and hashlib.sha256(current).digest() == after_hash:
                _replace(path, current, before)
                result = f"已撤销对 {name} 的最后一次文件修改。"
            else:
                return f"{name} 在修改后发生了变化，已保留当前内容。"
            conn.execute("DELETE FROM file_edits WHERE id = ?", (edit_id,))
            return result
    except (OSError, sqlite3.Error) as error:
        raise ToolExecutionError(f"无法撤销文件修改: {error}", tool_name="undo") from error
