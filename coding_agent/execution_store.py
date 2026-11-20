"""Persist in-flight turns and recover their history without replaying tools."""

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from exceptions import TurnPersistenceError
from session_store import _ensure_tables, _get_db_path, _write_session


def _ensure_execution_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS execution_turns (
            turn_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            owner_pid INTEGER NOT NULL,
            status TEXT NOT NULL,
            reason TEXT,
            history TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS execution_tools (
            turn_id TEXT NOT NULL,
            round INTEGER NOT NULL,
            call_id TEXT NOT NULL,
            request TEXT NOT NULL,
            status TEXT NOT NULL,
            exit_code INTEGER,
            verification TEXT,
            invalidates_verification INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (turn_id, round, call_id)
        )
    """)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(execution_tools)")}
    for name, definition in (("verification", "TEXT"), ("invalidates_verification", "INTEGER NOT NULL DEFAULT 0")):
        if name not in columns:
            try:
                conn.execute(f"ALTER TABLE execution_tools ADD COLUMN {name} {definition}")
            except sqlite3.OperationalError:
                if name not in {row[1] for row in conn.execute("PRAGMA table_info(execution_tools)")}:
                    raise
    conn.execute("CREATE INDEX IF NOT EXISTS idx_execution_turns_session ON execution_turns(session_id)")


def record_execution(event: dict, messages: list[dict]) -> None:
    """Commit each checkpoint before the next tool or model request."""
    turn_id = event["turn_id"]
    kind = event["type"]
    try:
        with closing(sqlite3.connect(_get_db_path())) as conn, conn:
            _ensure_execution_tables(conn)
            if kind == "turn_start":
                conn.execute(
                    "INSERT INTO execution_turns VALUES (?, ?, ?, 'running', NULL, ?)",
                    (turn_id, event["session_id"], os.getpid(), json.dumps(messages)),
                )
                return
            if kind == "turn_end":
                changed = conn.execute(
                    """UPDATE execution_turns SET status = ?, reason = ?, history = NULL
                       WHERE turn_id = ? AND status = 'running'""",
                    (event["status"], event["reason"], turn_id),
                )
            else:
                changed = conn.execute(
                    "UPDATE execution_turns SET history = ? WHERE turn_id = ? AND status = 'running'",
                    (json.dumps(messages), turn_id),
                )
            if changed.rowcount != 1:
                raise TurnPersistenceError("执行记录已关闭或不存在。")

            key = (turn_id, event.get("round"), event.get("tool_call_id"))
            if kind == "tool_request":
                conn.execute(
                    """INSERT INTO execution_tools (turn_id, round, call_id, request, status)
                       VALUES (?, ?, ?, ?, 'started')""",
                    (*key, json.dumps(event["call"])),
                )
            elif kind == "tool_result":
                outcome = event["outcome"]
                updated = conn.execute(
                    """UPDATE execution_tools SET status = ?, exit_code = ?, verification = ?, invalidates_verification = ?
                       WHERE turn_id = ? AND round = ? AND call_id = ? AND status = 'started'""",
                    (outcome["status"], outcome["exit_code"], outcome.get("verification"),
                     int(bool(outcome.get("invalidates_verification"))), *key),
                )
                if updated.rowcount != 1:
                    raise TurnPersistenceError("找不到待记录结果的工具调用。")
    except (sqlite3.Error, ValueError, TypeError) as error:
        raise TurnPersistenceError(f"无法保存执行记录：{error}") from error


def execution_facts(session_id: str) -> tuple[dict | None, list[str]]:
    with closing(sqlite3.connect(_get_db_path())) as conn, conn:
        _ensure_execution_tables(conn)
        latest = conn.execute(
            """SELECT e.rowid, e.call_id, e.verification, e.exit_code
               FROM execution_tools e JOIN execution_turns t ON e.turn_id=t.turn_id
               WHERE t.session_id=? AND e.verification IS NOT NULL ORDER BY e.rowid DESC LIMIT 1""",
            (session_id,),
        ).fetchone()
        verification = None
        if latest:
            rowid, call_id, status, exit_code = latest
            stale = conn.execute(
                """SELECT 1 FROM execution_tools e JOIN execution_turns t ON e.turn_id=t.turn_id
                   WHERE t.session_id=? AND e.rowid>? AND e.invalidates_verification LIMIT 1""",
                (session_id, rowid),
            ).fetchone()
            verification = {"call_id": call_id, "status": "stale" if stale else status, "exit_code": exit_code}
        uncertain = [row[0] for row in conn.execute(
            """SELECT e.call_id FROM execution_tools e JOIN execution_turns t ON e.turn_id=t.turn_id
               WHERE t.session_id=? AND e.status IN ('started', 'interrupted', 'timeout', 'uncertain')
               ORDER BY e.rowid DESC LIMIT 8""",
            (session_id,),
        )]
    return verification, uncertain


def _process_running(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    synchronize = 0x00100000
    handle = kernel.OpenProcess(synchronize, False, pid)
    if not handle:
        # ERROR_INVALID_PARAMETER indicates that the PID no longer exists.
        return ctypes.get_last_error() != 87
    try:
        return kernel.WaitForSingleObject(handle, 0) != 0
    finally:
        kernel.CloseHandle(handle)


def _tool_history(conn: sqlite3.Connection, turn_id: str) -> list[dict]:
    rows = conn.execute(
        """SELECT request, status, exit_code, round FROM execution_tools
           WHERE turn_id = ? ORDER BY rowid""",
        (turn_id,),
    )
    return [
        {
            "call": json.loads(request), "status": status, "exit_code": exit_code,
            "round": round_number,
        }
        for request, status, exit_code, round_number in rows
    ]


def _recovery_history(history: str, tools: list[dict]) -> tuple[list[dict], list[dict]]:
    messages = json.loads(history)
    tool_ids = set()
    index = len(messages) - 1
    while index >= 0 and messages[index]["role"] == "tool":
        tool_ids.add(messages[index]["tool_call_id"])
        index -= 1
    if index < 0 or messages[index]["role"] != "assistant":
        return messages, []

    uncertain = {step["call"]["id"] for step in tools if step["status"] == "started"}
    unresolved = []
    for call in messages[index].get("tool_calls", []):
        if call["id"] in tool_ids:
            continue
        if call["id"] in uncertain:
            status = "unknown"
            text = "上轮退出时该工具没有持久化结果。先核查文件或命令的实际效果，再决定是否重试。"
        else:
            status = "not_started"
            text = "上轮退出前未开始执行该工具。"
        messages.append({"role": "tool", "tool_call_id": call["id"], "content": text})
        unresolved.append({"call": call, "status": status, "exit_code": None})
    return messages, unresolved


def pending_executions() -> list[dict]:
    path = _get_db_path()
    if not Path(path).is_file():
        return []
    try:
        with closing(sqlite3.connect(path)) as conn, conn:
            _ensure_execution_tables(conn)
            conn.execute("BEGIN")
            turns = conn.execute(
                """SELECT turn_id, session_id, owner_pid, history FROM execution_turns
                   WHERE status = 'running' ORDER BY rowid DESC"""
            ).fetchall()
            pending = []
            for turn_id, session_id, pid, history in turns:
                tools = _tool_history(conn, turn_id)
                _, unresolved = _recovery_history(history, tools)
                pending.append({
                    "turn_id": turn_id, "session_id": session_id,
                    "active": pid != os.getpid() and _process_running(pid),
                    "tools": [step for step in tools if step["status"] != "started"] + unresolved,
                })
            return pending
    except (sqlite3.Error, ValueError) as error:
        raise TurnPersistenceError(f"无法读取执行记录：{error}") from error


def execution_session(turn_id: str) -> str | None:
    """Return the stable session id recorded for a turn."""
    path = _get_db_path()
    if not Path(path).is_file():
        return None
    try:
        with closing(sqlite3.connect(path)) as conn:
            row = conn.execute(
                "SELECT session_id FROM execution_turns WHERE turn_id = ?", (turn_id,),
            ).fetchone()
            return row[0] if row else None
    except sqlite3.Error:
        return None


def recover_execution(turn_id: str) -> list[dict] | None:
    path = _get_db_path()
    if not Path(path).is_file():
        return None
    try:
        with closing(sqlite3.connect(path)) as conn, conn:
            _ensure_tables(conn)
            _ensure_execution_tables(conn)
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT owner_pid, history, session_id FROM execution_turns
                   WHERE turn_id = ? AND status = 'running'""",
                (turn_id,),
            ).fetchone()
            if row is None:
                return None
            pid, history, session_id = row
            if pid != os.getpid() and _process_running(pid):
                raise TurnPersistenceError("该轮所属进程可能仍在运行，待其退出后再恢复。")
            messages, _ = _recovery_history(history, _tool_history(conn, turn_id))
            _write_session(conn, "__auto__", messages, uid=session_id)
            conn.execute(
                "UPDATE execution_turns SET status = 'recovered', history = NULL WHERE turn_id = ?",
                (turn_id,),
            )
            return messages
    except (sqlite3.Error, ValueError) as error:
        raise TurnPersistenceError(f"无法恢复执行记录：{error}") from error
