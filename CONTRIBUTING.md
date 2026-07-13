# Contributing

Thanks for helping improve Codex Task Watchdog.

## Principles

1. Keep the watchdog local-first, lightweight, and standard-library-only unless a dependency has a compelling safety benefit.
2. Prefer evidence and notification over automatic action.
3. Never automatically replay side-effecting or quota-spending work.
4. Keep parallel calls isolated by exact call IDs or opaque watchdog tags.
5. Never delete or rewrite Codex databases, sessions, rollouts, generated images, worktrees, or project assets.
6. Keep outputs bounded so monitoring cannot itself overload an agent context.

## Tests

```powershell
python -m unittest discover -s skills\codex-watchdog\scripts -p 'test_*.py' -v
```

Add synthetic fixtures for every parser change. Do not copy real Codex log rows, IDs, task labels, paths, prompts, or outputs into tests.

## Privacy check before committing

Inspect tracked filenames:

```powershell
git ls-files | rg -i '(__pycache__|\.pyc$|\.sqlite|\.jsonl|\.env($|\.)|generated_images|recovery_manifests|sessions|archived_sessions)'
```

Scan content for common private values and absolute paths:

```powershell
git grep -nI -E '(C:\\Users\\|/Users/|/home/|AppData|Desktop|D:\\|AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_|BEGIN [A-Z ]+PRIVATE KEY|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})'
```

Review every match. Documentation may intentionally mention generic path shapes, but source, fixtures, and examples must never contain a contributor's real home path, email, token, task ID, or runtime record.

If available, also run `gitleaks git . --redact` and `trufflehog filesystem . --only-verified` before publishing.

## Pull requests

- Explain which observed state or failure mode the change addresses.
- State whether the change can mutate files, send messages, spend quota, start processes, or alter startup settings.
- Update `README.md`, the protocol reference, schemas, and tests when behavior changes.
- Keep log discovery and output narrowly bounded.
