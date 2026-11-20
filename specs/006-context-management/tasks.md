# Tasks — Nano Code 上下文管理

## Setup and foundational work

- [x] T001 Inventory runtime, adapters, storage, commands and baseline tests in research.md.

## Phase 1 — Budget and tool results

- [x] T002 [US1] Add failing budget, usage and pruning cases in coding_agent/tests/test_context_management.py.
- [x] T003 [US1] Resolve model limits, estimates and actual provider usage in coding_agent/context_budget.py and the adapters.
- [x] T004 [US1] Add tool-specific context views, bounded replay and terminal metrics in coding_agent/nano_code/context_pruning.py and coding_agent/nano_code/commands.py.

## Phase 2 — Compaction

- [x] T005 [US2] Cover manual, automatic, failure, repetition and tool-followup cases in coding_agent/tests/test_context_management.py and test_release.py.
- [x] T006 [US2] Build and persist safe structured request views in coding_agent/nano_code/context.py, runtime.py and sessions.py.

## Phase 3 — Memory and caching

- [x] T007 [US3] Cover task evidence, selective memory, cached usage and recovery in coding_agent/tests/test_context_management.py.
- [x] T008 [US3] Connect working memory, prefix caching and restart compatibility in coding_agent/nano_code/memory.py, providers.py and runtime.py.

## Phase 4 — Cross-cutting validation

- [x] T009 Execute provider and workflow regressions in coding_agent/tests.
- [x] T010 Measure same-workload resource use in evals/context_benchmark.py and document the outcomes in evals/README.md.
- [x] T011 Document user commands, limits, raw history and configuration in coding_agent/README.md.

## Dependencies

Foundation → US1 → US2 → US3 → validation. Within a story, write the regression cases before implementation. Budget accounting and output fixtures can be developed independently once their contracts are in place. Provider and CLI regressions exercise the shared runtime after integration.
