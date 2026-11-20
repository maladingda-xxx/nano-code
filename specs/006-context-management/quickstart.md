# Quickstart — Verify Context Management

1. Install `coding_agent[dev]` into a Python 3.11+ environment.
2. Run `python -m pytest coding_agent/tests` from the repository root.
3. Open Nano Code and run `/context` and `/context stats`. Create several turns or a tool-heavy turn and run `/compact`.
4. Resume the same UID and check that `/context` shows the prior compaction and export contains the original messages.
5. Run `python evals/context_benchmark.py` for the offline comparison. Follow [evals/README.md](../../evals/README.md) for a provider-backed comparison.
