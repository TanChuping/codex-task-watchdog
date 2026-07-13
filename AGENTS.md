# Repository instructions

- Read `skills/codex-watchdog/SKILL.md` and `skills/codex-watchdog/references/protocol.md` before changing behavior.
- Keep the project standard-library-only unless maintainers explicitly accept a dependency.
- Use only synthetic log events, paths, IDs, and labels in tests and documentation.
- Never commit Codex databases, runtime state, incidents, recovery manifests, session files, rollouts, generated images, credentials, proxy settings, or absolute contributor paths.
- Preserve the safety boundary: notify and record; do not automatically retry tools, send prompts, spend quota, kill Codex, or delete task data.
- Keep parallel attempts isolated by exact `call_id` or full opaque watchdog tag.
- Run `python -m unittest discover -s skills/codex-watchdog/scripts -p "test_*.py" -v` after changes.
- Update tests, protocol documentation, and schemas together when state-machine behavior changes.
