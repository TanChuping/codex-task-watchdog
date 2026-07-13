from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Codex task health check")
    parser.add_argument("--thread", help="Thread id; defaults to CODEX_THREAD_ID")
    args = parser.parse_args(argv)
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    thread_id = (args.thread or os.environ.get("CODEX_THREAD_ID", "")).strip()
    database = codex_home / "state_5.sqlite"

    if not thread_id:
        print(json.dumps({"status": "unknown", "reason": "CODEX_THREAD_ID is not set"}))
        return 0
    if not database.exists():
        print(json.dumps({"status": "unknown", "reason": "state database not found"}))
        return 0

    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT rollout_path, tokens_used, model, reasoning_effort "
            "FROM threads WHERE id = ?",
            (thread_id,),
        ).fetchone()
    finally:
        connection.close()

    if row is None:
        print(json.dumps({"status": "unknown", "thread_id": thread_id}))
        return 0

    rollout_path, tokens_used, model, reasoning_effort = row
    tokens_used = int(tokens_used or 0)
    rollout_bytes = 0
    if rollout_path:
        try:
            rollout_bytes = os.path.getsize(rollout_path)
        except OSError:
            pass

    if tokens_used >= 100_000_000 or rollout_bytes >= 500_000_000:
        status = "critical"
    elif tokens_used >= 50_000_000 or rollout_bytes >= 250_000_000:
        status = "warning"
    else:
        status = "healthy"

    print(
        json.dumps(
            {
                "status": status,
                "thread_id": thread_id,
                "tokens_used": tokens_used,
                "rollout_bytes": rollout_bytes,
                "rollout_path": rollout_path,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "recommended_action": (
                    "write_handoff_and_start_clean_without_forking_history"
                    if status == "critical"
                    else "checkpoint_at_next_milestone"
                    if status == "warning"
                    else "continue"
                ),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
