"""Compare legacy request shaping with ContextManager on repeatable offline tasks."""

import json
import re
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "coding_agent"))

from agent_core import ModelReply, run_turn
from context_budget import context_breakdown, estimate_tokens, fit_request
from nano_code.config import Config
from nano_code.context import ContextManager, SUMMARY_FIELDS
from nano_code.runtime import schema

WINDOW = 20000
TOOLS = [schema("execute_command", "Return the log from a fixed test suite.",
                {"argv": {"type": "array", "items": {"type": "string"}}}, ["argv"])]


def failure_marker(number):
    return f"FAILED test_group_{number}::test_regression"


def reported_failures(messages):
    return {
        int(match) for message in messages
        for match in re.findall(r"FAILED test_group_(\d+)::test_regression", message.get("content") or "")
    }


def evaluates(name, rounds, log_length, manager):
    events = []
    calls = 0
    repeated = 0
    estimated_input = 0
    estimated_output = 0
    requests = 0
    attempted_operations = set()

    def summarize(summary_messages, emit):
        nonlocal estimated_input, estimated_output, requests
        requests += 1
        estimated_input += context_breakdown(summary_messages, [], limit=WINDOW, model="unknown").input_tokens
        summary = {key: "Unknown" for key in SUMMARY_FIELDS}
        summary["current_task"] = "Identify the exact failing test in each group."
        summary["findings"] = "\n".join(failure_marker(i) for i in sorted(reported_failures(summary_messages)))
        text = json.dumps(summary)
        estimated_output += estimate_tokens([{"role": "assistant", "content": text}], "unknown")
        return text

    def request(messages, allow_tools, emit):
        nonlocal estimated_input, estimated_output, requests, repeated
        active_tools = TOOLS if allow_tools else []
        view = messages
        if manager:
            view, _ = manager.view(messages, allow_tools)
            if manager.budget(view, allow_tools).pressure >= manager.config.data["context_compact_at"]:
                manager.compact(messages, summarize, emit, lambda _: None, trigger="automatic", allow_tools=allow_tools)
                view, _ = manager.view(messages, allow_tools)
        fitted, _ = fit_request(view, active_tools, limit=WINDOW, max_output=4096, emit=emit,
                                safety=manager.config.model_budget().safety if manager else 0, model="unknown")
        requests += 1
        estimated_input += context_breakdown(fitted, active_tools, limit=WINDOW, model="unknown").input_tokens
        found = reported_failures(fitted)
        text = "\n".join(failure_marker(i) for i in sorted(found))
        operation = next((i for i in range(1, rounds + 1) if i not in found), None)
        if operation is None or not allow_tools:
            text = text or "No failure identified."
            estimated_output += estimate_tokens([{"role": "assistant", "content": text}], "unknown")
            return ModelReply(text=text)
        repeated += int(operation in attempted_operations)
        attempted_operations.add(operation)
        call = {
            "id": f"call-{requests}", "type": "function",
            "function": {"name": "execute_command", "arguments": json.dumps({"argv": ["fixture", str(operation)]})},
        }
        estimated_output += estimate_tokens([{"role": "assistant", "content": text, "tool_calls": [call]}], "unknown")
        return ModelReply(text=text or None, tool_calls=[call])

    def dispatch(call):
        nonlocal calls
        calls += 1
        number = int(json.loads(call["function"]["arguments"])["argv"][1])
        rows = "synthetic test runner progress 0123456789\n" * log_length
        return "Exit code: 1\n" + rows + failure_marker(number) + "\n" + rows + "1 failed"

    history = [{"role": "system", "content": "Report the exact failures for the current task."}]
    started = perf_counter()
    status = run_turn(history, f"Identify all {rounds} failing tests in order.", request_model=request,
                      dispatch=dispatch, emit=events.append, max_tool_rounds=rounds + 4)
    accepted = status == "completed" and all(
        failure_marker(i) in history[-1]["content"] for i in range(1, rounds + 1)
    )
    return {
        "task": name, "passed": accepted, "estimated_input_tokens": estimated_input,
        "estimated_output_tokens": estimated_output, "requests": requests, "tool_calls": calls,
        "repeated_operations": repeated, "elapsed_ms": round((perf_counter() - started) * 1000, 2),
        "compactions": sum(e["type"] == "context_compaction" and e["status"] == "completed" for e in events),
        "cache_read_tokens": None, "api_cost": None,
    }


def main():
    with TemporaryDirectory() as directory:
        config = Config(Path(directory), overrides={
            "provider": "openai", "base_url": "https://example.test", "model": "offline-fixture",
            "context_limit": WINDOW, "context_models": {}, "context_safety_tokens": 512,
            "context_tool_result_chars": 3600, "context_prune_at": 0.20, "context_compact_at": 0.30,
        })
        tasks = (("short", 1, 2), ("long_tool_loop", 9, 95))
        baseline = [evaluates(*task, manager=None) for task in tasks]
        managed = [evaluates(*task, manager=ContextManager(config, TOOLS)) for task in tasks]
    print(json.dumps({"mode": "offline_scripted", "baseline": baseline, "managed": managed}, indent=2))
    return 0 if all(result["passed"] for result in [*baseline, *managed]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
