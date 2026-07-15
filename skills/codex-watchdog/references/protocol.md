# Watchdog protocol

## Entry point

Resolve `<WD_SCRIPT>` to the absolute path of `scripts/codex_watchdog.py` beside this skill. The script accepts global `--home PATH` and `--json` options.

| Command | Purpose |
|---|---|
| `enable [--db PATH] [--poll-seconds N] [--post-tool-seconds N] [--response-seconds N] [--critical-seconds N] [--opaque-model-seconds N] [--stream-silence-seconds N] [--tool-warning-seconds N] [--tool-critical-seconds N] [--log-batch-rows N] [--no-notify] [--no-start]` | Persistently enable monitoring; start the hidden watcher unless `--no-start` is given. Opaque model preparation defaults to 600 seconds, quiet streams to 900 seconds, tool warning to 180 seconds, and tool review to 600 seconds. Log batches are capped at 2,000 rows. |
| `disable` | Persistently disable monitoring; the watcher exits on its next check. |
| `start` | Start the hidden watcher when monitoring is enabled. |
| `run [--once]` | Run the watcher in the foreground, or perform one polling pass. |
| `status` | Show configuration, watcher state, and active-job counts. |
| `arm --kind KIND [--thread ID] [--turn ID\|auto] [--timeout-seconds N] [--label TEXT] [--generation N]` | Register one attempt and return its unique tag. The thread defaults to `CODEX_THREAD_ID`; `--turn auto` infers the latest matching turn or creates a unique logical turn label. `--timeout-seconds` is a caller-selected rolling no-progress threshold; choose it using `timeout-policy.md`, not as a universal total-runtime cap. |
| `heartbeat TAG [--note TEXT]` | Record a live observation for exactly one attempt. |
| `disarm TAG [--reason TEXT]` | Close exactly one attempt. Always supply a useful terminal reason. |
| `list [--limit N] [--all]` | List active jobs by default. Output is capped at 500 records even with compatibility `--all`. |
| `incidents [--limit N] [--all]` | Inspect recent deduplicated incidents. Output is capped at 500 records even with compatibility `--all`. |
| `recover-plan --thread ID [--turn ID]` | Build one bounded, read-only, same-task-first recovery decision. It never wakes, retries, stops, or creates anything. |
| `cleanup --dry-run\|--apply` | Preview or apply retention only inside the watchdog runtime directory. Never touches Codex task data. |
| `test-notify [--message TEXT]` | Test local notification delivery without arming work. |
| `install [--task-name NAME] [--dry-run]` | Install the per-user `HKCU` logon startup entry without elevation. |
| `uninstall [--task-name NAME] [--dry-run]` | Remove the per-user startup entry. |

Invoke commands as `python <WD_SCRIPT> COMMAND ...`. Use `--json` when consuming output programmatically. Never guess a tag or select “the latest” job as a shortcut.

## Tag format

`arm` is the sole tag generator. Its returned form is:

```text
[CODEX-WATCHDOG|thread=<urlquoted>|turn=<urlquoted>|kind=<urlquoted>|uuid=<uuid>|generation=<n>]
```

Treat the entire string as opaque. `thread`, `turn`, and `kind` aid diagnosis; `uuid` supplies uniqueness; `generation` separates retries. Preserve quoting exactly when passing the tag back to the CLI.

## Job state machine

```text
arm --> ARMED -- chosen interval expires --> STALLED (review due)
          ^                                      |
          | heartbeat TAG --note EVIDENCE        | confirmed abnormality
          +--------------------------------------+--> DISARMED

ARMED or STALLED -- result/failure/cancel --> DISARMED
```

- Keep global `enabled`/`disabled` separate from job state. `disable` prevents new monitoring but does not turn a prior tool result into a failure.
- Treat a 30-second observation cadence as a check opportunity only. The selected threshold is rolling time since the last verified progress, not an absolute wall-clock cap. On expiry, the `stalled` state means mandatory human/agent review, not automatic termination. If current scan, stream, log, tool-call, file, process, worker-phase, incomplete model output, or task-specific evidence is healthy, record an evidence-bearing heartbeat and continue. Do not let synthetic timer ticks or an unchanged “thinking” label reset it. See `timeout-policy.md` for task-class defaults and override rules.
- Never infer that an agent stopped from completed command records alone. The detector recognizes `model_preparing` between a tool completion and the next `/responses` request; this may include context assembly or code/patch construction that produces no file change yet.
- Every absence-only incident carries `confirmed_failure: false` and `safe_to_interrupt: false`. Long gaps escalate from `warning` to `review`, not to a failure verdict. Only explicit terminal/failure evidence or user instruction can authorize interruption.
- On every terminal or confirmed-stall path, call `disarm`. Record reasons such as `completed`, `failed: <cause>`, `cancelled`, or `confirmed stalled: <evidence>`.
- Never revive a disarmed tag. Arm a retry with an incremented generation; do not replay side-effecting work automatically.

## Parallel isolation

- Create one job per tool call or worker attempt, even when several belong to one batch.
- Store and route events by the full exact tag. Never update jobs by `kind`, label, thread, or list position.
- Ignore late heartbeats and results from an older generation after its tag is disarmed.
- Disarm only the job that completed or stalled; leave sibling jobs untouched.
- Include the tag in progress and incident notes so user-visible messages can be matched to the correct worker.
- Persist manifest updates atomically. Validate `jobs.json` against `manifest.schema.json` when changing its format.

## Recovery boundary

The watcher is an external timer. It can inspect persisted state, record incidents, and issue a local notification. It cannot force a continuation through an app-server scheduling failure, a disconnected client, or a network stall.

After reconnecting, run `status`, `list --limit 50`, and `incidents --limit 20`, then inspect the real output location before retrying. A missing UI event is not proof that a tool failed. Retry only the missing attempt, use a new generation, and require explicit user authorization unless an active instruction already pre-authorizes that retry.

For task recovery, run `recover-plan`, inspect the live task with the Codex thread/task interface, and prefer one continuation message to the same task only after it is confirmed terminal or idle and unfinished. If it is active or opaque, leave it untouched. Do not have the monitoring task redo the target task. Use a small disk handoff and a clean task only when same-task recovery is impossible or health is `critical`.

Use bounded `list` and `incidents` output in model context; compatibility `--all` exists only for older callers and remains capped. A model-preparation event, new `/responses` request, stream event, terminal event, or matching completion clears stale transition tracking. A call with no completion produces `tool_running_no_completion` after 180 seconds and a `review` incident after 600 seconds; review incidents reuse one metadata-only recovery manifest for the same incident state. Quiet opaque model preparation waits 600 seconds and quiet model streams wait 900 seconds before review by default.

Parallel calls keep separate `call_id` incident records, but notifications are grouped by task/turn/type/severity and simultaneous critical calls in one turn share one recovery manifest.

If `check_thread_health.py` reports `critical`, do not fork the old task. Write a concise project handoff that records repository/worktree state, output paths and hashes, verification, one next action, safe retries, and forbidden repeats. Continue in a clean task from those disk pointers. Do not load a giant rollout to make the handoff.

Retention is deliberately narrow: active/stalled jobs are never pruned; disarmed jobs are retained for 30 days and capped at the newest 500; incident logs rotate at 5 MiB with three backups. Never use this cleanup path for session rollouts, `logs_2.sqlite`, `state_5.sqlite`, worktrees, project assets, or generated images.
