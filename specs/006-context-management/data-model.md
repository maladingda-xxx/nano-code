# Data Model — Context Management

- Canonical history contains ordered system instructions, exact user prompts, assistants and original tool text. Session UID and tool call ID retain their existing semantics.
- Context snapshot holds a non-system message count, digest, nine summary sections, before and after estimates, trigger, timestamp and successful compaction count. The digest binds a snapshot to a specific history prefix.
- Working memory combines exact current request, tool results with recorded status, changed paths, verification status and checklist items. Recent message groups remain available as short-term memory.
- Project memory entries are bounded Markdown sections. Their labels and dates are preserved and the contents are treated as project data.
- Usage totals include only available provider metrics. Cached input and reasoning are parts of totals with provider-specific normalization.
