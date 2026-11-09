"""Drive the N09/N10 recovery procedures in isolated checkouts."""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from run import CACHE, ROOT, checkout

AGENT = ROOT / "coding_agent" / "agent.py"
ENV_FILE = ROOT / "coding_agent" / ".env"
SETTINGS = {
    "MAX_MODEL_REQUESTS": "9",
    "MAX_TOOL_ROUNDS": "8",
    "MAX_RETRIES": "1",
    "CONTEXT_LIMIT_TOKENS": "80000",
    "STREAMING_ENABLED": "false",
    "TEMPERATURE": "0",
    "TOP_P": "1",
}
N09_PROMPT = "只在根 README 末尾追加独立一行 RECOVERY-PROBE，然后报告改动。"
N10_PROMPT = "只在根 README 末尾追加独立一行 UNDO-PROBE，然后报告改动。"


def agent_env():
    env = {
        **os.environ, **SETTINGS,
        "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8",
    }
    for key in ("API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
        env.pop(key, None)
    return env


def query(destination, sql, params=()):
    path = destination / ".coding_agent_sessions.db"
    if not path.is_file():
        return []
    try:
        with sqlite3.connect(path) as conn:
            return conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []


def readme_bytes(destination):
    return (destination / "README.md").read_bytes()


def readme_text(destination):
    return readme_bytes(destination).decode("utf-8", errors="replace")


def usage_summary(output):
    values = [int(value) for value in re.findall(r"用量 (\d+) tokens", output)]
    return {"requests": len(values), "total_tokens": sum(values)} if values else None


class AgentProcess:
    """Run the interactive CLI with piped input and a prompt responder."""

    def __init__(self, destination):
        self.lines = []
        self.condition = threading.Condition()
        self.write_lock = threading.Lock()
        self.proc = subprocess.Popen(
            [sys.executable, "-B", str(AGENT), "--env-file", str(ENV_FILE)],
            cwd=destination, env=agent_env(), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            for line in self.proc.stdout:
                with self.condition:
                    self.lines.append(line)
                    self.condition.notify_all()
                if "恢复上次会话？" in line:
                    self.send("n")
                elif "执行此命令？" in line:
                    self.send("n")
        except (OSError, ValueError):
            return

    def send(self, text):
        with self.write_lock:
            try:
                self.proc.stdin.write(text + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass

    def wait_for(self, predicate, timeout):
        deadline = time.monotonic() + timeout
        with self.condition:
            while time.monotonic() < deadline:
                if predicate("".join(self.lines)):
                    return True
                self.condition.wait(min(0.1, max(0.0, deadline - time.monotonic())))
        return False

    def output(self):
        with self.condition:
            return "".join(self.lines)

    def kill_tree(self):
        if self.proc.poll() is None:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                    capture_output=True, timeout=10,
                )
            else:
                self.proc.kill()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def wait(self, timeout=15):
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.kill_tree()
            return None


def fresh_checkout(task_id):
    report_dir = Path(tempfile.mkdtemp(prefix=f"{task_id}-", dir=CACHE))
    destination = report_dir / "checkout"
    checkout({"id": task_id, "repository": "nano-code"}, destination)
    return report_dir, destination


def latest_turn(destination):
    rows = query(
        destination,
        "SELECT turn_id, status FROM execution_turns ORDER BY rowid DESC LIMIT 1",
    )
    return rows[0] if rows else (None, None)


def turn_tools(destination, turn_id):
    return query(
        destination,
        "SELECT call_id, status, exit_code FROM execution_tools WHERE turn_id = ? ORDER BY rowid",
        (turn_id,),
    )


def session_messages(destination, name="__auto__"):
    return query(
        destination,
        """SELECT m.role, m.content FROM messages m
           JOIN sessions s ON s.id = m.session_id
           WHERE s.name = ? ORDER BY m.seq""",
        (name,),
    )


def wait_until(predicate, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def run_n09():
    report_dir, destination = fresh_checkout("N09")
    readme = destination / "README.md"
    agent = AgentProcess(destination)
    agent.send(N09_PROMPT)
    written = wait_until(
        lambda: "RECOVERY-PROBE" in readme_text(destination), timeout=180,
    )
    killed_at = round(time.monotonic(), 3)
    agent.kill_tree()
    (report_dir / "first-run.txt").write_text(agent.output(), encoding="utf-8")

    first_text = readme_text(destination)
    turn_id, turn_status = latest_turn(destination)
    tools = turn_tools(destination, turn_id) if turn_id else []
    write_status = tools[-1][1] if tools else None

    recovered = False
    recover_output = ""
    recovered_messages = []
    if written and turn_id:
        restarted = AgentProcess(destination)
        time.sleep(1.0)
        restarted.send(f"/recover {turn_id}")
        recovered = wait_until(
            lambda: query(
                destination,
                "SELECT status FROM execution_turns WHERE turn_id = ?",
                (turn_id,),
            )[:1] == [("recovered",)],
            timeout=30,
        )
        restarted.send("/exit")
        restarted.wait(15)
        recover_output = restarted.output()
        (report_dir / "recover-run.txt").write_text(recover_output, encoding="utf-8")
        recovered_messages = session_messages(destination)

    marker_count = first_text.count("RECOVERY-PROBE")
    tool_text = "\n".join(
        content or "" for role, content in recovered_messages if role == "tool"
    )
    consistent = (
        (write_status == "started" and ("没有持久化结果" in tool_text or "待核查" in tool_text))
        or (write_status == "ok" and any(
            marker in tool_text for marker in ("已写入", "已编辑", "RECOVERY-PROBE")
        ))
        or (write_status is None and "RECOVERY-PROBE" in first_text)
    )
    passed = bool(written and marker_count == 1 and recovered and consistent)
    report = {
        "task": "N09", "result": "passed" if passed else "failed",
        "scripted": True, "human_interventions": 0,
        "written_before_kill": written, "marker_count": marker_count,
        "turn_id": turn_id, "turn_status_before_recover": turn_status,
        "write_tool_status": write_status,
        "recovered": recovered, "recovery_consistent": consistent,
        "killed_at": killed_at, "usage": usage_summary(agent.output()), "settings": SETTINGS,
        "first_run_output": "first-run.txt", "recover_output": "recover-run.txt",
        "readme": "readme.txt",
    }
    (report_dir / "readme.txt").write_text(first_text, encoding="utf-8")
    (report_dir / "result.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    print(f"N09 {report['result']} · marker {marker_count} · write {write_status} · {report_dir}")
    return passed, report_dir


def run_n10():
    report_dir, destination = fresh_checkout("N10")
    readme = destination / "README.md"
    readme.write_bytes(b"USER-BEFORE\n" + readme_bytes(destination))
    agent = AgentProcess(destination)
    agent.send(N10_PROMPT)

    def turn_finished():
        turn_id, status = latest_turn(destination)
        return "UNDO-PROBE" in readme_text(destination) and status == "completed"

    finished = wait_until(turn_finished, timeout=180)
    turn_id, turn_status = latest_turn(destination)
    (report_dir / "task-run.txt").write_text(agent.output(), encoding="utf-8")

    first_ok = second_ok = False
    first_text = second_text = ""
    edits_after_first = edits_after_second = 0
    if finished:
        readme.write_bytes(readme_bytes(destination) + b"USER-AFTER\n")
        agent.send("/undo")
        time.sleep(1.0)
        first_bytes = readme_bytes(destination)
        first_text = first_bytes.decode("utf-8", errors="replace")
        edits_after_first = len(query(destination, "SELECT id FROM file_edits"))
        first_ok = (
            "USER-BEFORE" in first_text and "USER-AFTER" in first_text
            and "UNDO-PROBE" in first_text and edits_after_first > 0
        )
        readme.write_bytes(first_bytes.replace(b"USER-AFTER\n", b""))
        agent.send("/undo")
        second_ok = wait_until(
            lambda: "UNDO-PROBE" not in readme_text(destination), timeout=15,
        )
        second_text = readme_text(destination)
        edits_after_second = len(query(destination, "SELECT id FROM file_edits"))
        second_ok = (
            second_ok and "USER-BEFORE" in second_text
            and "USER-AFTER" not in second_text and "UNDO-PROBE" not in second_text
            and edits_after_second == 0
        )
    agent.send("/exit")
    agent.wait(15)
    (report_dir / "undo-run.txt").write_text(agent.output(), encoding="utf-8")

    passed = bool(finished and first_ok and second_ok)
    report = {
        "task": "N10", "result": "passed" if passed else "failed",
        "scripted": True, "human_interventions": 0,
        "turn_id": turn_id, "turn_status": turn_status,
        "first_undo_kept_current": first_ok,
        "second_undo_removed_probe": second_ok,
        "file_edits_after_first": edits_after_first,
        "file_edits_after_second": edits_after_second,
        "usage": usage_summary(agent.output()), "settings": SETTINGS,
        "task_output": "task-run.txt", "undo_output": "undo-run.txt",
        "readme": "readme.txt",
    }
    (report_dir / "readme.txt").write_text(second_text, encoding="utf-8")
    (report_dir / "result.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    print(f"N10 {report['result']} · first {first_ok} · second {second_ok} · {report_dir}")
    return passed, report_dir


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", choices=["N09", "N10", "both"], nargs="?", default="both")
    parser.add_argument("--env-file", type=Path, help="accepted for compatibility; uses coding_agent/.env")
    args = parser.parse_args()
    if args.env_file is not None and not args.env_file.is_file():
        print("Configuration file not found.", file=sys.stderr)
        return 2
    if not ENV_FILE.is_file():
        print("coding_agent/.env is required for recovery runs.", file=sys.stderr)
        return 2
    CACHE.mkdir(parents=True, exist_ok=True)
    results = []
    if args.task in {"N09", "both"}:
        results.append(run_n09()[0])
    if args.task in {"N10", "both"}:
        results.append(run_n10()[0])
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
