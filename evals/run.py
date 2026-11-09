"""Reproduce fixed fixtures and record one live evaluation at a time."""

import argparse
import hashlib
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
TASKS = {task["id"]: task for task in SUITE["tasks"]}
PILOT = "I01"
LIVE_TASKS = tuple(task["id"] for task in SUITE["tasks"] if task["type"] != "recovery")
BASE_SETTINGS = {
    "MAX_MODEL_REQUESTS": "5",
    "MAX_TOOL_ROUNDS": "4",
    "MAX_RETRIES": "1",
    "CONTEXT_LIMIT_TOKENS": "12000",
    "STREAMING_ENABLED": "false",
    "TEMPERATURE": "0",
    "TOP_P": "1",
}
TASK_SETTINGS = {
    "bug": {
        "MAX_MODEL_REQUESTS": "9", "MAX_TOOL_ROUNDS": "8",
        "CONTEXT_LIMIT_TOKENS": "80000",
    },
    "feature": {
        "MAX_MODEL_REQUESTS": "9", "MAX_TOOL_ROUNDS": "8",
        "CONTEXT_LIMIT_TOKENS": "80000",
    },
}
TASK_OVERRIDES = {
    "N02": {
        "MAX_MODEL_REQUESTS": "9", "MAX_TOOL_ROUNDS": "8",
        "CONTEXT_LIMIT_TOKENS": "80000",
    },
}


def settings_for(task):
    return {
        **BASE_SETTINGS, **TASK_SETTINGS.get(task["type"], {}),
        **TASK_OVERRIDES.get(task["id"], {}),
    }


def allowed_paths(task):
    if task["type"] == "read":
        return set()
    if "mutation" in task:
        return {task["mutation"]["path"]}
    return set(task["allowed_paths"])


def format_paths(paths):
    return "、".join(f"`{path}`" for path in sorted(paths))


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
    for key in (*BASE_SETTINGS, "API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY",
                "BASE_URL", "MODEL_NAME", "COMMAND_TIMEOUT_SECONDS"):
        env.pop(key, None)
    completed = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", *task["verify"]],
        cwd=destination, env=env, capture_output=True, encoding="utf-8", errors="replace",
        timeout=90,
    )
    if expected_exit is not None and completed.returncode != expected_exit:
        raise RuntimeError(
            f"{task['id']} expected pytest exit {expected_exit}, got {completed.returncode}\n"
            f"{completed.stdout}\n{completed.stderr}"
        )
    return completed


def inject_regression(task, destination):
    mutation = task["mutation"]
    path = destination / mutation["path"]
    text = path.read_text(encoding="utf-8")
    if text.count(mutation["old"]) != 1:
        raise RuntimeError(f"{task['id']} mutation no longer matches exactly once")
    path.write_text(text.replace(mutation["old"], mutation["new"]), encoding="utf-8")


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
                inject_regression(task, destination)
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


def untracked_files(destination, *, ignore_fixture=False):
    generated = {".coding_agent_sessions.db"}
    if ignore_fixture:
        generated.add("nano_eval_test.py")
    return [
        path for path in git(destination, "ls-files", "--others", "-z", trim=False).split("\0")
        if path and path not in generated and not path.startswith(".coding_agent_sessions.db-")
    ]


def fixture_sha256(destination):
    path = destination / "nano_eval_test.py"
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def prepare_task(task, destination, report_dir):
    if "fixture" in task:
        shutil.copyfile(EVAL_DIR / "checks" / task["fixture"], destination / "nano_eval_test.py")
    if task["type"] == "read":
        return None
    if task["type"] == "feature":
        failing = verify(task, destination, expected_exit=1)
        (report_dir / "baseline-tests.txt").write_text(
            failing.stdout + failing.stderr, encoding="utf-8"
        )
        return None
    baseline = verify(task, destination, expected_exit=0)
    inject_regression(task, destination)
    seeded = verify(task, destination, expected_exit=1)
    (report_dir / "baseline-tests.txt").write_text(baseline.stdout + baseline.stderr, encoding="utf-8")
    (report_dir / "seeded-tests.txt").write_text(seeded.stdout + seeded.stderr, encoding="utf-8")
    seeded_diff = git(destination, "diff", "--no-ext-diff", "--binary", "HEAD", trim=False)
    (report_dir / "seeded.diff").write_text(seeded_diff, encoding="utf-8")
    return seeded_diff


def build_prompt(task):
    prompt = task["prompt"]
    if task["type"] == "read":
        return prompt
    check = "python -B -m pytest -q -p no:cacheprovider " + " ".join(task["verify"])
    paths = format_paths(allowed_paths(task))
    if task["type"] == "bug":
        prompt += f"\n评测器已复现失败，验证命令为 `{check}`。请只修改 {paths}。"
    else:
        prompt += f"\n评测器已放置验收测试 `nano_eval_test.py`，当前失败。请只修改 {paths}。"
    prompt += "本轮由评测器在检查补丁后独立执行测试，请不要尝试执行命令。请完成改动并说明待验证内容。"
    return prompt


def run_live(env_file, task_id=PILOT):
    config, key = load_config(env_file)
    task = TASKS[task_id]
    if task["type"] == "recovery":
        raise RuntimeError(f"{task_id} 需要按 tasks.json 的 procedure 手工执行。")
    settings = settings_for(task)
    report_dir = Path(tempfile.mkdtemp(prefix=f"{task_id}-", dir=CACHE))
    destination = report_dir / "checkout"
    checkout(task, destination)
    seeded_diff = prepare_task(task, destination, report_dir)
    fixture_digest = fixture_sha256(destination)
    prompt = build_prompt(task)
    command = [sys.executable, "-B", str(ROOT / "coding_agent" / "agent.py"),
               "-p", prompt, "--json"]
    if env_file.is_file():
        command.extend(["--env-file", str(env_file)])
    env = {**os.environ, **settings, "PYTHONIOENCODING": "utf-8"}
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
    untracked = untracked_files(destination, ignore_fixture="fixture" in task)
    fixture_changed = fixture_digest is not None and fixture_sha256(destination) != fixture_digest
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
    completed_turn = (
        exit_code == 0 and end.get("status") == "completed" and bool(replies)
        and 1 <= len(requests) <= int(settings["MAX_MODEL_REQUESTS"])
    )
    if task["type"] == "read":
        passed = (
            completed_turn and all(item in replies[-1] for item in task["answer_contains"])
            and consulted and set(actions) <= allowed_tools and not diff and not untracked
        )
        result = "passed" if passed else "failed"
    else:
        changed = set(git(destination, "diff", "--name-only", "HEAD").splitlines())
        has_change = bool(diff) or task["type"] == "bug"
        candidate = (
            completed_turn and has_change and diff != seeded_diff and not untracked
            and not fixture_changed and changed <= allowed_paths(task)
        )
        result = "pending_verification" if candidate else "failed"
    host = urlsplit(config.get("BASE_URL") or "https://api.deepseek.com").hostname
    model = config.get("MODEL_NAME") or ("deepseek-flash" if host == "api.deepseek.com" else "deepseek-chat")
    report = {
        "task": task_id, "task_type": task["type"], "result": result,
        "target_revision": git(destination, "rev-parse", "HEAD"),
        "agent_revision": git(ROOT, "rev-parse", "HEAD"),
        "agent_tree_dirty": bool(git(ROOT, "status", "--short", "--", "coding_agent")),
        "provider_host": host, "model": model, "settings": settings,
        "allowed_paths": sorted(allowed_paths(task)), "prompt": prompt,
        "elapsed_seconds": elapsed, "requests": len(requests),
        "usage": {field: sum(event[field] for event in usage)
                  for field in ("input_tokens", "output_tokens", "total_tokens")} if usage else None,
        "human_interventions": 0, "tool_calls": actions, "verification": end.get("verification"),
        "turn_reason": end.get("reason"), "exit_code": exit_code,
        "unexpected_untracked": untracked, "fixture_sha256": fixture_digest,
        "fixture_changed": fixture_changed, "final_diff": "final.diff",
    }
    (report_dir / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{task_id} {result} · {len(requests)} requests · {report_dir / 'result.json'}")
    return 1 if result == "failed" else 0


def verify_candidate(run_name):
    if Path(run_name).name != run_name:
        raise RuntimeError("Expected a report directory name, not a path.")
    directory = CACHE / run_name
    report_path = directory / "result.json"
    if not report_path.is_file():
        raise RuntimeError("Report directory not found.")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    task = TASKS.get(report.get("task"))
    if task is None or task["type"] == "read":
        raise RuntimeError("This report has no candidate awaiting verification.")
    if not run_name.startswith(f"{task['id']}-") or report["result"] != "pending_verification":
        raise RuntimeError("This report has no candidate awaiting verification.")
    destination = directory / "checkout"
    diff = git(destination, "diff", "--no-ext-diff", "--binary", "HEAD", trim=False)
    changed = set(git(destination, "diff", "--name-only", "HEAD").splitlines())
    if (git(destination, "rev-parse", "HEAD") != SUITE["repositories"][task["repository"]]["commit"]
            or diff != (directory / "final.diff").read_text(encoding="utf-8")
            or untracked_files(destination, ignore_fixture="fixture" in task)
            or not changed <= allowed_paths(task)):
        raise RuntimeError("The candidate has changed since the model run.")
    expected_fixture = report.get("fixture_sha256")
    if expected_fixture is not None and fixture_sha256(destination) != expected_fixture:
        raise RuntimeError("The acceptance fixture has changed since the model run.")
    completed = verify(task, destination, expected_exit=None)
    (directory / "final-tests.txt").write_text(completed.stdout + completed.stderr, encoding="utf-8")
    report["independent_verification_exit_code"] = completed.returncode
    report["result"] = "passed" if completed.returncode == 0 else "failed"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{task['id']} {report['result']} · pytest exit {completed.returncode} · {report_path}")
    return 0 if completed.returncode == 0 else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["preflight", "live", "verify"])
    parser.add_argument("--task", choices=LIVE_TASKS, default=PILOT)
    parser.add_argument("--run", help="pending report directory to verify after reviewing the candidate")
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    CACHE.mkdir(parents=True, exist_ok=True)
    try:
        if args.mode == "preflight":
            return preflight()
        if args.mode == "verify":
            if not args.run:
                parser.error("verify requires --run")
            return verify_candidate(args.run)
        if args.env_file is not None and not args.env_file.is_file():
            raise RuntimeError(f"Configuration file not found: {args.env_file}")
        return run_live((args.env_file or ROOT / "coding_agent" / ".env").resolve(), args.task)
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as error:
        print(error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
