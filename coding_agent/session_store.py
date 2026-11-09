"""
session_store.py —— 会话持久化（SQLite）
========================================

提供对话历史的保存/恢复能力，让 Agent 退出后可以接着上次的对话继续。

特性：
  - 自动保存：每轮完成或中断后写入 SQLite
  - 启动恢复：检测到上次会话时可选择继续
  - 命名保存：支持 /save <名称> 手动命名保存
  - 加载会话：支持 /load <名称> 加载已命名会话
  - 列出会话：支持 /sessions 列出所有已保存会话

数据库位置：项目根目录下的 .coding_agent_sessions.db
"""

import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path

_workspace_root: Path | None = None


def set_session_root(root: Path) -> None:
    global _workspace_root
    _workspace_root = root


def _get_db_path() -> str:
    """获取 SQLite 数据库文件路径。"""
    return str((_workspace_root or Path.cwd()) / ".coding_agent_sessions.db")


def _readable_db_paths():
    """Prefer repository sessions, then pre-v0.3 sessions in the launch directory."""
    primary = Path(_get_db_path())
    legacy = Path.cwd() / primary.name
    for path in dict.fromkeys((primary, legacy)):
        if path.is_file():
            yield path


def _ensure_tables(conn: sqlite3.Connection) -> None:
    """创建必需的表（如果不存在）。"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            message_count INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            tool_calls TEXT,
            tool_call_id TEXT,
            name TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
    """)
    conn.commit()


# ============================================================
# 保存会话
# ============================================================

def save_session(name: str, messages: list[dict]) -> bool:
    """
    将当前消息列表保存为一个命名的会话。

    参数:
        name:     会话名称
        messages: 消息列表

    返回:
        是否保存成功
    """
    db_path = _get_db_path()
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        _ensure_tables(conn)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 检查是否存在同名会话
        existing = conn.execute(
            "SELECT id FROM sessions WHERE name = ?", (name,)
        ).fetchone()

        if existing:
            session_id = existing[0]
            # 删除旧消息
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute(
                "UPDATE sessions SET updated_at = ?, message_count = ? WHERE id = ?",
                (now, 0, session_id),
            )
        else:
            cursor = conn.execute(
                "INSERT INTO sessions (name, created_at, updated_at, message_count) VALUES (?, ?, ?, ?)",
                (name, now, now, 0),
            )
            session_id = cursor.lastrowid

        # 插入消息
        for seq, msg in enumerate(messages):
            tool_calls_json = None
            if msg.get("tool_calls"):
                tool_calls_json = json.dumps(msg["tool_calls"], ensure_ascii=False)

            conn.execute(
                """INSERT INTO messages
                   (session_id, seq, role, content, tool_calls, tool_call_id, name, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    seq,
                    msg.get("role", ""),
                    msg.get("content"),
                    tool_calls_json,
                    msg.get("tool_call_id"),
                    msg.get("name"),
                    now,
                ),
            )

        conn.execute(
            "UPDATE sessions SET updated_at = ?, message_count = ? WHERE id = ?",
            (now, len(messages), session_id),
        )
        conn.commit()
        return True
    except (sqlite3.Error, TypeError, ValueError) as e:
        print(f"保存会话失败: {e}", file=sys.stderr)
        return False
    finally:
        if conn is not None:
            conn.close()


# ============================================================
# 自动保存（当前会话）
# ============================================================

def auto_save(messages: list[dict]) -> bool:
    """
    自动保存当前会话到默认槽位（名称固定为 '__auto__'）。
    在每轮完成或中断后自动调用。

    参数:
        messages: 当前消息列表

    返回:
        是否保存成功
    """
    return save_session("__auto__", messages)


# ============================================================
# 加载会话
# ============================================================

def load_session(name: str) -> list[dict] | None:
    """
    加载指定名称的会话消息列表。

    参数:
        name: 会话名称

    返回:
        消息列表，如果会话不存在则返回 None
    """
    for db_path in _readable_db_paths():
        messages = _load_session_from(db_path, name)
        if messages is not None:
            return messages
    return None


def _load_session_from(db_path: Path, name: str) -> list[dict] | None:
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        _ensure_tables(conn)
        session = conn.execute(
            "SELECT id, message_count FROM sessions WHERE name = ? ORDER BY updated_at DESC LIMIT 1",
            (name,),
        ).fetchone()

        if not session:
            return None

        session_id, _ = session
        rows = conn.execute(
            "SELECT role, content, tool_calls, tool_call_id, name FROM messages WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()

        messages = []
        for row in rows:
            role, content, tool_calls_json, tool_call_id, tool_name = row
            msg = {"role": role}
            if content is not None:
                msg["content"] = content
            if tool_calls_json:
                msg["tool_calls"] = json.loads(tool_calls_json)
            if tool_call_id:
                msg["tool_call_id"] = tool_call_id
            if tool_name:
                msg["name"] = tool_name
            messages.append(msg)

        return messages
    except (sqlite3.Error, ValueError) as e:
        print(f"加载会话失败: {e}", file=sys.stderr)
        return None
    finally:
        if conn is not None:
            conn.close()


def has_auto_saved_session() -> bool:
    return bool(load_session("__auto__"))


def list_saved_sessions() -> list[dict]:
    """List sessions with repository entries taking precedence over legacy ones."""
    sessions = {}
    for db_path in _readable_db_paths():
        try:
            with closing(sqlite3.connect(db_path)) as conn:
                _ensure_tables(conn)
                rows = conn.execute(
                    "SELECT name, created_at, updated_at, message_count FROM sessions"
                ).fetchall()
        except sqlite3.Error:
            continue
        for name, created, updated, count in rows:
            sessions.setdefault(name, {
                "name": name, "created_at": created, "updated_at": updated, "message_count": count,
            })
    return sorted(sessions.values(), key=lambda session: session["updated_at"], reverse=True)


def delete_session(name: str) -> bool:
    """
    删除指定名称的会话。

    参数:
        name: 会话名称

    返回:
        是否删除成功
    """
    db_path = _get_db_path()
    if not os.path.exists(db_path):
        return False

    conn = sqlite3.connect(db_path)
    _ensure_tables(conn)
    conn.execute("PRAGMA foreign_keys = ON")

    try:
        conn.execute("DELETE FROM sessions WHERE name = ?", (name,))
        conn.commit()
        return True
    except Exception as e:
        print(f"删除会话失败: {e}")
        return False
    finally:
        conn.close()
