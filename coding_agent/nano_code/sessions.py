"""UID-addressed sessions and process leases, compatible with v0.3 databases."""

import json
import os
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from uuid import uuid4

import session_store as legacy


def save(uid, messages, name=None):
    # Every process writes its own slot, never the shared legacy __auto__ slot.
    slot = "__session_" + uid
    if not legacy.save_session(slot, messages, uid=uid):
        return False
    if name:
        rename(uid, name)
    return True


def _connect():
    conn = sqlite3.connect(legacy._get_db_path(), timeout=10)
    legacy._ensure_tables(conn)
    conn.execute("CREATE TABLE IF NOT EXISTS session_labels (uid TEXT PRIMARY KEY, label TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS session_leases (uid TEXT PRIMARY KEY, pid INTEGER NOT NULL, token TEXT NOT NULL)")
    conn.commit()
    return conn


def list_sessions():
    entries = legacy.list_saved_sessions()
    if not entries:
        return []
    with closing(_connect()) as conn, conn:
        labels = dict(conn.execute("SELECT uid, label FROM session_labels"))
    result = {}
    # Canonical saves win over stale named snapshots, even within the same second.
    for entry in sorted(entries, key=lambda s: (s["name"].startswith("__session_"), s["updated_at"]), reverse=True):
        uid = entry["uid"]
        if uid in result:
            continue
        result[uid] = {**entry, "slot": entry["name"], "name": labels.get(uid) or (
            entry["name"] if not entry["name"].startswith("__") else f"Session {uid[:8]}")}
    return sorted(result.values(), key=lambda entry: entry["updated_at"], reverse=True)


def load(reference=None):
    entries = list_sessions()
    matches = entries[:1] if not reference else [
        s for s in entries if s["uid"].startswith(reference) or s["name"] == reference or s["slot"] == reference
    ]
    if len(matches) > 1:
        raise ValueError("会话名称或 ID 不唯一，请使用完整 ID。")
    if not matches:
        return None
    entry = matches[0]
    record = legacy.load_session_record(entry["slot"])
    return {**entry, **record} if record else None


def rename(uid, label):
    if not isinstance(label, str) or not label.strip() or len(label) > 120:
        raise ValueError("会话名称长度须为 1–120 个字符。")
    with closing(_connect()) as conn, conn:
        conn.execute("INSERT INTO session_labels VALUES (?, ?) ON CONFLICT(uid) DO UPDATE SET label=excluded.label", (uid, label.strip()))


def fork(uid, messages, label=None):
    new_uid = uuid4().hex
    if not save(new_uid, messages, label or f"Fork of {uid[:8]}"):
        raise ValueError("无法保存分支会话。")
    return new_uid


def _alive(pid):
    from execution_store import _process_running
    return _process_running(pid)


@contextmanager
def lease(uid):
    token = uuid4().hex
    with closing(_connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        owner = conn.execute("SELECT pid FROM session_leases WHERE uid=?", (uid,)).fetchone()
        if owner and _alive(owner[0]):
            raise ValueError("该会话正在另一个任务中运行。请等待结束或使用 --fork-session。")
        conn.execute("INSERT OR REPLACE INTO session_leases VALUES (?, ?, ?)", (uid, os.getpid(), token))
    try:
        yield
    finally:
        with closing(_connect()) as conn, conn:
            conn.execute("DELETE FROM session_leases WHERE uid=? AND token=?", (uid, token))


def export_session(uid, messages, path):
    path = Path(path)
    if path.exists():
        raise ValueError("导出文件已存在，请使用新文件名。")
    if path.suffix == ".json":
        text = json.dumps({"schema_version": 1, "session_id": uid, "messages": messages}, ensure_ascii=False, indent=2)
    else:
        text = f"# Nano Code session {uid}\n\n" + "\n\n".join(
            f"## {m['role']}\n\n{m.get('content') or ''}" for m in messages if m["role"] != "system")
    path.write_text(text + "\n", encoding="utf-8")
    return str(path)
