# Codex Task Watchdog

Local-first stall detection and safe recovery for long-running OpenAI Codex tasks.

[![Tests](https://github.com/TanChuping/codex-task-watchdog/actions/workflows/tests.yml/badge.svg)](https://github.com/TanChuping/codex-task-watchdog/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)

**Codex Task Watchdog** is an unofficial, Windows-first **OpenAI Codex watchdog** and installable **Codex skill**. It detects stalled tool calls, missing SSE response activity, and long-running tasks; diagnoses oversized Codex threads under context pressure; and writes metadata-only recovery handoffs. It is deliberately conservative: it records evidence and notifies you, but never automatically retries a quota-spending or side-effecting tool, sends a prompt, kills Codex, or deletes task data.

The project is useful when Codex Desktop remains on “Thinking”, a tool call never returns to the agent, a batch silently stops progressing, or an old task has accumulated too much history to continue safely.

> Experimental compatibility note: Codex's local log schema and log messages are implementation details and may change. This project currently targets the Windows Codex Desktop layout that provides `logs_2.sqlite` and `state_5.sqlite`.

## At a glance

| It does | It intentionally does not |
|---|---|
| Reads matching technical events from the local Codex log database in SQLite read-only mode | Modify Codex databases, rollouts, sessions, or project files |
| Detects response-stream and tool-completion stalls | Treat every slow model response as a failure |
| Tracks explicit long operations with unique `arm` / `heartbeat` / `disarm` tags | Guess which parallel call completed |
| Produces local Windows notifications and bounded incident records | Send telemetry or upload logs |
| Diagnoses oversized tasks and creates metadata-only recovery manifests | Fork or compact giant task histories |
| Prunes only watchdog-owned metadata under strict retention rules | Automatically retry tools, spend quota, or repeat side effects |

## Why this exists

Several different failures look identical in the UI:

- the `/responses` request was made but no SSE stream began;
- a tool call started but no matching completion arrived;
- a tool completed but the next model request never began;
- the client or app-server disconnected, so a timer cannot wake the active agent;
- the task is technically alive but its accumulated context is now operationally risky.

Open Codex reports describe related symptoms, including [long periods without child-agent health/progress signals](https://github.com/openai/codex/issues/16900) and [Codex Desktop remaining on Thinking while Stop fails](https://github.com/openai/codex/issues/24287). The watchdog supplies external evidence and a safe recovery boundary; it does not claim to repair the Codex scheduler or network connection itself.

## How it works

```text
Codex logs_2.sqlite (read-only)        Explicit long operation
            |                                 |
       filtered events                 arm -> heartbeat
            |                                 |
      per-turn state machine          unique opaque tag
            +---------------+-----------------+
                            |
                  threshold + deduplication
                            |
              incident + Windows notification
                            |
             metadata-only recovery manifest
```

The default detector thresholds are:

| Signal | Warning | Critical |
|---|---:|---:|
| Completed tool, but no next response request | 45 seconds | — |
| Response requested, but no SSE activity | 120 seconds | 180 seconds |
| Tool started, but no matching completion | 180 seconds | 600 seconds |
| Explicitly armed operation with no verified progress | selected by the main conversation for that task class | same rolling no-progress threshold |

A new response request, stream event, terminal event, or matching completion clears stale state. Parallel calls remain isolated by `call_id` and explicit jobs use unique tagged generations. Automatic log-state warnings only notify and record; they do not kill a task.

For explicitly armed work, 180 seconds is no longer a universal limit. The main conversation selects a **rolling no-progress threshold** based on the expected silent interval:

| Task class | Starting threshold |
|---|---:|
| Fast interactive/network work | 180 seconds |
| Ordinary commands | 300 seconds |
| Heavy builds, tests, downloads, CI, or rendering | 600 seconds |
| Delegated research or bounded batches | 900 seconds |
| Explicitly sparse marathon work | 1800 seconds or a justified custom value |

These values are not total runtime caps. Expiry is a mandatory review point: the main conversation or a dedicated monitoring Agent inspects the exact attempt. Advancing scan counters, fresh stream/log/tool-call records, changing files, active process work, or a worker phase transition can justify an evidence-bearing heartbeat and another interval. A static “Thinking” label by itself cannot. A long task can therefore run indefinitely while healthy progress continues. The current image workflow remains a deliberate exception: one image attempt keeps its separate 180-second hard watchdog. See the full [timeout selection policy](skills/codex-watchdog/references/timeout-policy.md).

## Requirements

- Windows 10 or 11 for background startup and desktop notifications
- Python 3.10 or newer
- Codex Desktop with a local Codex home (normally `%USERPROFILE%\.codex`)
- No third-party Python packages

The core parser and test suite also run on non-Windows systems, but background startup and notifications are Windows-specific.

## Five-minute quick start

Clone and copy the skill into your user Codex home:

```powershell
git clone https://github.com/TanChuping/codex-task-watchdog.git
Set-Location codex-task-watchdog
.\scripts\install.ps1
```

The installer only copies the skill by default. It does **not** enable monitoring or change startup settings unless you ask it to:

```powershell
.\scripts\install.ps1 -Enable -InstallStartup
```

Or run each decision separately:

```powershell
$CodexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }
$Watchdog = Join-Path $CodexHome 'skills\codex-watchdog\scripts\codex_watchdog.py'

python $Watchdog status
python $Watchdog enable
python $Watchdog install --dry-run
python $Watchdog install
python $Watchdog test-notify
```

To turn it off globally in any later Codex task, ask Codex to **“disable watchdog”** / **“关闭 watchdog”**, or run:

```powershell
python $Watchdog disable
```

Disabling persists. Removing the per-user startup entry is separate and reversible:

```powershell
python $Watchdog uninstall --dry-run
python $Watchdog uninstall
```

The included [`SKILL.md`](skills/codex-watchdog/SKILL.md) teaches Codex when to enable, disable, arm, heartbeat, disarm, inspect, clean, and create a handoff. For a repository-scoped installation, copy `skills/codex-watchdog` to `.agents/skills/codex-watchdog` in that repository. See the [official Codex skills documentation](https://developers.openai.com/codex/skills) for current skill discovery locations.

## Monitor one long operation

Every attempt gets its own opaque tag. The main conversation first selects the task-appropriate no-progress threshold, then keeps the returned tag exactly as printed:

```powershell
$Arm = python $Watchdog --json arm `
  --kind image-generation `
  --turn auto `
  --generation 1 `
  --timeout-seconds 180 `
  --label 'concept-03' | ConvertFrom-Json
$Tag = $Arm.tag

# Record a heartbeat after the main/monitoring Agent judges current evidence healthy.
python $Watchdog heartbeat $Tag --note 'new output file observed'

# Always close the exact attempt on every terminal path.
python $Watchdog disarm $Tag --reason 'completed'
```

For retries, use a new tag and increment `--generation`. Never select “the latest job” as a shortcut when tools run in parallel.

## Oversized task diagnosis

The bundled health checker reads the current task record and rollout file size without opening or rewriting the rollout:

```powershell
$Health = Join-Path $CodexHome 'skills\codex-watchdog\scripts\check_thread_health.py'
python $Health
python $Health --thread '<thread-id>'
```

It returns `healthy`, `warning`, `critical`, or `unknown`. A critical result means: checkpoint the real project state, write a concise handoff, and continue in a clean task from disk pointers. Do not fork a huge history merely to preserve conversational context.

## Recovery and reconnect rules

After a reconnect, interrupted UI turn, or missing notification:

1. Inspect `status`, `list --limit 50`, and `incidents --limit 20`.
2. Inspect the real output directory or repository state.
3. Treat a missing UI notification as inconclusive; the tool may already have completed.
4. Retry only the missing item with a fresh tag/generation.
5. Require explicit authorization before replaying anything that spends quota or causes side effects.

The external daemon cannot force the main Codex task to wake through an app-server, scheduler, client, or network disconnect. It can preserve evidence and alert the user so recovery is informed rather than blind.

## Data, safety, and privacy

Everything stays local. The watchdog opens `logs_2.sqlite` with SQLite `mode=ro` and `PRAGMA query_only=ON`, and requests only the technical log targets needed by its state machine. Runtime metadata is stored under:

```text
$CODEX_HOME/watchdog/
├── config.json
├── jobs.json
├── state.json
├── incidents.jsonl
└── recovery_manifests/
```

These files can contain private thread IDs, turn IDs, call IDs, timestamps, local rollout paths, job labels, and incident details. **Do not paste raw runtime output into a public issue.** Redact identifiers and paths first; see [SECURITY.md](SECURITY.md).

Cleanup is intentionally narrow:

- active and stalled jobs are never pruned;
- disarmed jobs are retained for 30 days and capped at the newest 500;
- incident logs rotate at 5 MiB with three backups;
- cleanup refuses to operate outside `$CODEX_HOME/watchdog`;
- Codex databases, rollouts, sessions, generated images, worktrees, and project assets are never cleanup targets.

Preview before applying:

```powershell
python $Watchdog cleanup --dry-run
python $Watchdog cleanup --apply
```

## CLI reference

| Command | Purpose |
|---|---|
| `enable` / `disable` | Persistently enable or disable monitoring |
| `start` / `run [--once]` | Start a hidden watcher or run it in the foreground |
| `status` | Show configuration, daemon state, and active-job counts |
| `arm` / `heartbeat` / `disarm` | Track one exact long-running attempt |
| `list` | List jobs with bounded output |
| `incidents` | Inspect recent deduplicated incidents with bounded output |
| `cleanup --dry-run\|--apply` | Prune watchdog-owned metadata only |
| `test-notify` | Send a harmless local notification test |
| `install` / `uninstall` | Add or remove the per-user Windows logon entry |

Run `python $Watchdog --help` or read the complete [protocol reference](skills/codex-watchdog/references/protocol.md).

## Related work

The name “Codex watchdog” is already used by several useful but differently scoped projects:

- [elgabrielc/codex-watchdog](https://github.com/elgabrielc/codex-watchdog) observes Git worktrees, ancestry, and repository invariants.
- [sudo-relax/Codex-Watchdog](https://github.com/sudo-relax/Codex-Watchdog) restarts and optionally nudges the macOS Codex Desktop app.
- [ShuxinYang111/codex-watchdog](https://github.com/ShuxinYang111/codex-watchdog) resumes macOS Codex sessions after terminal API errors.
- [WHUEugene/codex-watchdog-public](https://github.com/WHUEugene/codex-watchdog-public) supervises tmux-based Codex CLI workers, missions, and coaches.
- [sinclairpan-git/codex-watchdog](https://github.com/sinclairpan-git/codex-watchdog) provides a larger long-task runtime and Feishu control plane.

This repository focuses on a small Windows-first, local event-state detector plus explicit per-attempt monitoring, oversized-thread diagnosis, bounded cleanup, and safe handoff recovery. It avoids automatic continuation by design.

## Limitations and non-goals

- This is an unofficial community project, not an OpenAI product or supported API.
- It relies on current local database tables and technical log strings, so future Codex versions may require parser updates.
- A quiet model may be slow rather than stuck; thresholds are evidence, not proof.
- It does not repair proxy, VPN, TUN, DNS, provider, or OpenAI service problems.
- It does not promise exactly-once tool execution after a disconnect.
- Automatic retries, automatic UI prompts, process killing, and destructive session cleanup are out of scope.

## Development

Run the standard-library test suite:

```powershell
python -m unittest discover -s skills\codex-watchdog\scripts -p 'test_*.py' -v
```

Before opening a pull request, also run the privacy checks documented in [CONTRIBUTING.md](CONTRIBUTING.md). Parser changes should add synthetic log fixtures covering start, progress, terminal, abort, reconnect, parallel-call, and stale-generation behavior.

## 中文简介

这是一个面向 Codex Desktop 长任务的本地轻量看门狗：检测响应流或工具调用长时间没有进展，给并行任务分配不混淆的唯一标签，诊断上下文过大的旧任务，并生成安全交接所需的元数据。它不会自动重试、不会自动发消息、不会杀死 Codex，也不会清理 Codex 的会话或工程文件。安装后，新任务里的 Agent 可根据 `SKILL.md` 理解“启用/关闭 watchdog”“卡死监控”“任务健康检查”“安全交接恢复”等请求。

## License

[MIT](LICENSE). Contributions are welcome.
