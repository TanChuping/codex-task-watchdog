# Security and privacy

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting feature when available. Do not open a public issue containing credentials, private Codex task data, local database contents, raw runtime state, or unredacted paths.

Include the smallest synthetic reproduction that demonstrates the problem. If a log fragment is essential, replace all thread, turn, call, process, and project identifiers with obviously synthetic values.

## Sensitive local data

The source repository contains no runtime data. An installed watchdog writes local metadata under `$CODEX_HOME/watchdog`. Depending on the incident, those files may contain:

- thread, turn, call, and process identifiers;
- task labels and timestamps;
- local rollout paths and file sizes;
- incident classifications and timing;
- the local Python executable or installed script path.

Never attach `logs_2.sqlite`, `state_5.sqlite`, session JSONL files, rollout files, `auth.json`, `.env` files, proxy configuration, or the complete watchdog runtime directory to a public issue.

## Security model

- Codex SQLite databases are opened in read-only/query-only mode.
- The watchdog does not upload telemetry or task data.
- It does not send prompts, retry tools, terminate Codex, or mutate project files.
- Windows startup uses the current user's `HKCU` Run entry and does not require elevation.
- Cleanup is restricted to watchdog-owned metadata and refuses a different resolved scope.
- Runtime JSON updates use atomic replacement and a cross-process lock.

This project parses local implementation details rather than a stable public API. Review every release before installing it into an environment that handles sensitive work.
