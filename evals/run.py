"""Reproduce the evaluation fixtures or run the authorized read-only pilot."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

EVAL_DIR = Path(__file__).resolve().parent
ROOT = EVAL_DIR.parent
CACHE = ROOT / ".cache" / "nano-eval"
SUITE = json.loads((EVAL_DIR / "tasks.json").read_text(encoding="utf-8"))
PILOT = "I01"
PILOT_SETTINGS = {
    "MAX_MODEL_REQUESTS": "5",
    "MAX_TOOL_ROUNDS": "4",
    "MAX_RETRIES": "1",
    "CONTEXT_LIMIT_TOKENS": "12000",
    "STREAMING_ENABLED": "false",
    "TEMPERATURE": "0",
    "TOP_P": "1",
}


def git(directory, *arguments, trim=True):
    output = subprocess.check_output(
        ["git", "-C", str(directory), *arguments],
        encoding="utf-8", errors="replace",
    )
    return output.strip() if trim else output


def checkout(task, destination):
    name = task["repository"]
    repo = SUITE["repositories"][name]
    source = ROOT
    if "url" in repo:
        source = CACHE / f"{name}-{repo['tag']}"
        if not source.exists():
            subprocess.run(
                ["git", "clone", "--quiet", "--depth", "1", "--branch", repo["tag"],
                 repo["url"], str(source)],
                check=True, timeout=60,
            )
    subprocess.run(
        ["git", "clone", "--quiet", "--shared", "--no-checkout", str(source), str(destination)],
        check=True, timeout=30,
    )
    git(destination, "checkout", "--quiet", "--detach", repo["commit"])
    if git(destination, "rev-parse", "HEAD") != repo["commit"]:
        raise RuntimeError(f"{task['id']} checkout did not match its pinned commit")


def verify(task, destination, expected_exit):
    repo = SUITE["repositories"][task["repository"]]
    env = {**os.environ, "PYTHONPATH": str(destination / repo["pythonpath"])}
    for key in (*PILOT_SETTINGS, "API_KEY", "BASE_URL", "MODEL_NAME", "COMMAND_TIMEOUT_SECONDS"):
        env.pop(key, None)
    completed = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", *task["verify"]],
        cwd=destination, env=env, capture_output=True, encoding="utf-8", errors="replace",
        timeout=90,
    )
    if completed.returncode != expected_exit:
        raise RuntimeError(
            f"{task['id']} expected pytest exit {expected_exit}, got {completed.returncode}\n"
            f"{completed.stdout}\n{completed.stderr}"
        )


def preflight():
    with tempfile.TemporaryDirectory(prefix="preflight-", dir=CACHE) as scratch:
        for task in SUITE["tasks"]:
            destination = Path(scratch) / task["id"]
            checkout(task, destination)
            if "fixture" in task:
                shutil.copyfile(EVAL_DIR / "checks" / task["fixture"], destination / "nano_eval_test.py")
            if task["type"] == "read":
                for path, expected in task["evidence"].items():
                    text = (destination / path).read_text(encoding="utf-8")
                    if not all(item in text for item in expected):
                        raise RuntimeError(f"{task['id']} evidence changed in {path}")
            elif task["type"] == "bug":
                verify(task, destination, expected_exit=0)
                mutation = task["mutation"]
                path = destination / mutation["path"]
                text = path.read_text(encoding="utf-8")
                if text.count(mutation["old"]) != 1:
                    raise RuntimeError(f"{task['id']} mutation no longer matches exactly once")
                path.write_text(text.replace(mutation["old"], mutation["new"]), encoding="utf-8")
                verify(task, destination, expected_exit=1)
            elif task["type"] == "feature":
                verify(task, destination, expected_exit=1)
            else:
                verify(task, destination, expected_exit=0)
            print(f"{task['id']} preflight OK", flush=True)
    return 0


def load_config(path):
    from dotenv import dotenv_values

    values = dotenv_values(path) if path.is_file() else {}
    config = {**values, **os.environ}
    key = (config.get("API_KEY") or "").strip()
    if not key or key == "sk-your-api-key-here":
        raise RuntimeError(f"Set API_KEY in {path} or in the process environment before the live pilot.")
    return config, key


def run_pilot(env_file):
    config, key = load_config(env_file)
    task = next(task for task in SUITE["tasks"] if task["id"] == PILOT)
    report_dir = Path(tempfile.mkdtemp(prefix=f"{PILOT}-", dir=CACHE))
    destination = report_dir / "checkout"
    checkout(task, destination)
    command = [sys.executable, "-B", str(ROOT / "coding_agent" / "agent.py"),
               "-p", task["prompt"], "--json"]
    if env_file.is_file():
        command.extend(["--env-file", str(env_file)])
    env = {**os.environ, **PILOT_SETTINGS, "PYTHONIOENCODING": "utf-8"}
    start = time.monotonic()
    try:
        completed = subprocess.run(
            command, cwd=destination, env=env, capture_output=True,
            encoding="utf-8", errors="replace", timeout=330,
        )
        stdout, stderr, exit_code = completed.stdout, completed.stderr, completed.returncode
    except subprocess.TimeoutExpired as error:
        stdout = (error.stdout or b"").decode("utf-8", errors="replace")
        stderr = (error.stderr or b"").decode("utf-8", errors="replace") + "\nPilot timed out."
        exit_code = None
    elapsed = round(time.monotonic() - start, 3)
    stdout, stderr = stdout.replace(key, "[redacted]"), stderr.replace(key, "[redacted]")
    (report_dir / "events.jsonl").write_text(stdout, encoding="utf-8")
    (report_dir / "stderr.txt").write_text(stderr, encoding="utf-8")
    try:
        events = [json.loads(line) for line in stdout.splitlines() if line]
    except ValueError:
        events = []
    diff = git(destination, "diff", "--no-ext-diff", "--binary", "HEAD", trim=False)
    (report_dir / "final.diff").write_text(diff, encoding="utf-8")
    generated = ".coding_agent_sessions.db"
    untracked = [
        path for path in git(destination, "ls-files", "--others", "--exclude-standard").splitlines()
        if path != generated and not path.startswith(generated + "-")
    ]
    replies = [event["text"] for event in events if event["type"] == "reply"]
    requests = [event for event in events if event["type"] == "model_request"]
    usage = [event for event in events if event["type"] == "usage"]
    end = next((event for event in reversed(events) if event["type"] == "turn_end"), {})
    allowed_tools = {"read_file", "search_workspace", "search_in_file", "list_directory"}
    actions = [event["call"]["function"]["name"] for event in events if event["type"] == "tool_request"]
    consulted = any(
        event["type"] == "tool_result" and event["outcome"]["status"] == "ok"
        and event["call"]["function"]["name"] in {"read_file", "search_workspace", "search_in_file"}
        for event in events
    )
    passed = (
        exit_code == 0 and end.get("status") == "completed" and bool(replies)
        and all(item in replies[-1] for item in task["answer_contains"])
        and consulted and set(actions) <= allowed_tools and not diff and not untracked
        and 1 <= len(requests) <= int(PILOT_SETTINGS["MAX_MODEL_REQUESTS"])
    )
    host = urlsplit(config.get("BASE_URL") or "https://api.deepseek.com").hostname
    model = config.get("MODEL_NAME") or ("deepseek-flash" if host == "api.deepseek.com" else "deepseek-chat")
    report = {
        "task": PILOT, "result": "passed" if passed else "failed",
        "target_revision": git(destination, "rev-parse", "HEAD"),
        "agent_revision": git(ROOT, "rev-parse", "HEAD"),
        "agent_tree_dirty": bool(git(ROOT, "status", "--short", "--", "coding_agent")),
        "provider_host": host, "model": model, "settings": PILOT_SETTINGS, "prompt": task["prompt"],
        "elapsed_seconds": elapsed, "requests": len(requests),
        "usage": {field: sum(event[field] for event in usage)
                  for field in ("input_tokens", "output_tokens", "total_tokens")} if usage else None,
        "human_interventions": 0, "tool_calls": actions, "verification": end.get("verification"),
        "turn_reason": end.get("reason"), "exit_code": exit_code,
        "unexpected_untracked": untracked, "final_diff": "final.diff",
    }
    (report_dir / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{PILOT} {report['result']} · {len(requests)} requests · {report_dir / 'result.json'}")
    return 0 if passed else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["preflight", "live"])
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    CACHE.mkdir(parents=True, exist_ok=True)
    try:
        if args.mode == "preflight":
            return preflight()
        if args.env_file is not None and not args.env_file.is_file():
            raise RuntimeError(f"Configuration file not found: {args.env_file}")
        return run_pilot((args.env_file or ROOT / "coding_agent" / ".env").resolve())
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
