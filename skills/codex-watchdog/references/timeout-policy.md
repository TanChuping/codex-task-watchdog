# Manual watchdog timeout policy

`arm --timeout-seconds` is the maximum allowed interval without **verified progress**. It is not the maximum total runtime of the task. A two-hour build can remain healthy if current process, output, file, worker, or network evidence keeps advancing.

The main conversation must classify every attempt expected to exceed 30 seconds and explicitly pass the selected value. Use these defaults as starting points, not as a substitute for judgment:

| Class | Typical work | Default no-progress threshold |
|---|---|---:|
| Fast interactive or network | tool discovery, ordinary API/web calls, UI operations, short remote requests | 180 seconds |
| Ordinary command | formatting, a focused test command, document conversion, modest filesystem work | 300 seconds |
| Heavy local or remote tool | full builds, large test suites, package installation, downloads, CI, rendering | 600 seconds |
| Delegated research or batch | subagents, repository research, multi-item processing with bounded workers | 900 seconds |
| Sparse marathon | explicitly long export, indexing, migration, or computation known to produce infrequent evidence | 1800 seconds or a justified custom value |

Image generation is a special case: when the active instructions impose a 180-second hard watchdog per image attempt, use 180 seconds, preserve completed images, and retry only the missing item in a fresh worker.

## Selection rules

1. Base the threshold on the longest normal silent interval for that tool, not on total expected duration.
2. Prefer a known tool timeout, historical run time, progress log cadence, worker status, output-file modification, or process activity as evidence.
3. If the task is unknown, use 300 seconds rather than assuming either instant failure or unlimited waiting.
4. The main conversation may choose a custom value and should state the reason in its progress update or job label.
5. Reclassify a live job only after new evidence shows the original class was wrong. Do not change the configured threshold merely because the timer is about to expire; use a supported heartbeat for healthy continuing work.
6. Keep observing at least every 30 seconds with a bounded wait. The main conversation or a dedicated monitoring subagent may judge progress from advancing scan counters, fresh SSE/log/tool-call records, file changes, active process work, worker phase transitions, or other task-specific signals. A timer tick or an unchanged “thinking” label alone is not progress.
7. On expiry, treat the incident as a mandatory review request. Inspect actual outputs and worker state. If evidence shows normal progress and no abnormal condition, send `heartbeat TAG --note EVIDENCE` and continue; the heartbeat restores a provisionally stalled job to `armed`. Stop or disarm only after confirmed abnormality or a terminal result. An incident never authorizes automatic replay, quota spending, prompt sending, or destructive cleanup.

Automatic log-state alerts (`post_tool_seconds`, `response_seconds`, `critical_seconds`, `tool_warning_seconds`, and `tool_critical_seconds`) are independent detector settings. They notify and record; they do not kill a task. Manual per-attempt classification controls only explicitly armed jobs.
