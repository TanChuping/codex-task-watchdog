---
name: codex-watchdog
description: Monitor long-running Codex tools and diagnose stalled or oversized tasks. Use for watchdog, 看门狗, stuck tool calls, tool timeouts, 卡死监控, session health, adaptive no-progress thresholds, bounded cleanup, safe handoff recovery, enabling or disabling local monitoring, or work expected to run longer than 30 seconds. Never auto-retry side effects or delete Codex data.
---

# Codex Watchdog

Use `scripts/codex_watchdog.py` as the only state-changing interface. Resolve it from this skill's directory and invoke it by absolute path; do not rely on the current working directory.

## Honor control requests

Treat these requests as executable controls, including in a brand-new conversation:

- On “关闭watchdog” or equivalent, run `python <WD_SCRIPT> disable`, then report the returned state.
- On “启用watchdog” or equivalent, run `python <WD_SCRIPT> enable`, then report the returned state.
- On a watchdog status request, run `python <WD_SCRIPT> status`, then report the result.
- On “卸载/彻底移除 watchdog”, run `disable` and then `uninstall`. Preserve the skill and incident files unless the user separately asks to delete them.

Do not merely acknowledge these requests. `disable` persists across conversations; while disabled, do not arm jobs unless the user enables monitoring again.

## Monitor long work

For each tool or delegated worker expected to exceed 30 seconds:

1. Check `status` once before an immediately launched batch. If enabled, classify each attempt and have the main conversation choose a rolling no-progress threshold from [references/timeout-policy.md](references/timeout-policy.md). Run `arm --kind KIND --turn auto --generation 1 --timeout-seconds CHOSEN-SECONDS --label SHORT-LABEL` for every initial attempt. The script uses `CODEX_THREAD_ID` when available and otherwise keeps an `unknown-thread` diagnostic label; tag UUIDs still isolate jobs. Retain the exact unique tag returned by the script.
2. Keep parallel work isolated: assign one tag to one attempt, and pass that exact tag to every later command.
3. Observe the real worker or output at least every 30 seconds. The main conversation or a dedicated monitoring subagent may record a heartbeat after judging the attempt healthy from current evidence such as advancing scan counters, new stream/log/tool-call records, changing output files, active process work, worker phase changes, or other task-specific progress. A timer tick or an unchanged “thinking” label alone is not proof of progress.
4. Treat expiry of the chosen interval as a mandatory review point, not an automatic stop. Notify or wake the main conversation/monitoring worker and inspect the exact attempt. If current evidence shows normal progress and no abnormal condition, run `heartbeat TAG --note EVIDENCE` and continue waiting; this re-arms a provisionally stalled job. If evidence confirms a real stall, preserve completed outputs and terminate only that worker when safe. A task may run much longer than `timeout_seconds` while it continues producing verified progress. Keep the separate 180-second hard limit for a single image-generation attempt when the active repository or user instructions require it.
5. Always run `disarm TAG --reason REASON` immediately on completion, failure, cancellation, or a confirmed abnormal stall. Use a new generation and a new tag for any retry.

Never automatically replay a tool that can spend quota, send messages, mutate files, or otherwise cause side effects unless an active user instruction explicitly pre-authorizes that retry. After a reconnect or interruption, inspect `status`, bounded `list`, bounded `incidents`, and actual outputs before deciding whether anything is genuinely missing. Compatibility `--all` output is still capped and must not be used as a routine context dump.

The external timer cannot penetrate an app-server, client, or network stall and cannot force the model to resume. It can persist evidence and notify the user; it is not proof that the Agent is alive.

## Recover oversized tasks

Run `python <SKILL_DIR>/scripts/check_thread_health.py` before substantial work after “continue” or a batch request. Resolve `<SKILL_DIR>` from this skill's own location; never hard-code a user profile path. Use `--thread ID` when diagnosing another task. On `critical`, do not continue or fork that task. Preserve repository and output files, write a small project handoff plus manifest, and start clean with only those paths after the user requests a new task.

Automatic critical incidents write metadata-only recovery manifests under `$CODEX_HOME/watchdog/recovery_manifests` (or `~/.codex/watchdog/recovery_manifests` when `CODEX_HOME` is unset). They may identify a rollout path and byte size but never read, rewrite, compact, or delete rollout contents. A manifest is diagnostic evidence, not authorization to retry.

## Keep watchdog metadata bounded

The daemon retains all active/stalled jobs, prunes disarmed jobs after 30 days or beyond the newest 500, and rotates `incidents.jsonl` at 5 MiB with three backups. Run `cleanup --dry-run` to inspect the exact plan; use `cleanup --apply` only when cleanup is requested. Both operations are restricted to the watchdog-owned runtime directory and must report `codex_data_touched: false`.

Read [references/protocol.md](references/protocol.md) before arming parallel jobs, handling a stall, or installing the per-user startup entry. Use [references/manifest.schema.json](references/manifest.schema.json) when reading or writing the job manifest.
Use [references/recovery-manifest.schema.json](references/recovery-manifest.schema.json) when consuming an automatic critical recovery manifest.
Read [references/timeout-policy.md](references/timeout-policy.md) before selecting or changing a manual job's no-progress threshold.
