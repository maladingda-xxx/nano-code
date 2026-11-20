# Context Interfaces

- `/context` shows next-request input estimate, window, output reserve, source breakdown and latest compaction.
- `/context stats` shows session counters with reported usage and estimation error.
- `/compact [focus]` attempts a summarization and describes the result. Cancellation and storage failures leave the old view available.
- `read_tool_output` reads a bounded page by message reference for the active session. Recorded text is subject to the original execution output limit and common credential masking on model replay.
- The normal JSON event stream carries pruning, compaction and usage details. Full history remains accessible via session resume and export.
