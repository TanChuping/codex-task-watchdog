#!/usr/bin/env python3
"""Lightweight, reversible watchdog for Codex Desktop.

The watchdog observes persisted Codex logs and explicitly armed long-running
jobs.  It records and notifies; it never retries tools, sends prompts, or
terminates Codex.
"""

from __future__ import annotations

import argparse
from collections import deque
import contextlib
import ctypes
import datetime as dt
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
import uuid


SCHEMA_VERSION = 1
DETECTOR_VERSION = 2
DEFAULT_TASK_NAME = "Codex Watchdog"
MAX_LOG_BATCH_ROWS = 2000
DEFAULT_LIST_LIMIT = 100
DEFAULT_INCIDENT_LIMIT = 20
MAX_OUTPUT_RECORDS = 500
DISARMED_RETENTION_SECONDS = 30 * 24 * 60 * 60
MAX_DISARMED_JOBS = 500
INCIDENT_MAX_BYTES = 5 * 1024 * 1024
INCIDENT_BACKUP_COUNT = 3
UUID_PATTERN = r"[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}"
TURN_SCOPE_RE = re.compile(
    rf'(?:^|:)turn\{{[^}}\r\n]*\bturn\.id=({UUID_PATTERN})(?:\s|\}})'
)
SAMPLING_TURN_RE = re.compile(
    rf'run_sampling_request\{{[^}}\r\n]*\bturn_id=({UUID_PATTERN})(?:\s|\}})'
)
EVENT_TURN_RE = re.compile(rf'\bturn_id=({UUID_PATTERN})(?:\s|$)')
PASSTHROUGH_TURN_RE = re.compile(rf'turn_id:\s*Some\("({UUID_PATTERN})"\)')
ABORT_RE = re.compile(rf':\s*aborting running task\b.*?\bsub_id="({UUID_PATTERN})"\s*$')
TOOL_START_RE = re.compile(
    r':try_run_sampling_request\{[^}\r\n]*\}:\s+Output item '
    r'item=(?:CustomToolCall|FunctionCall)\s+\{[^\r\n]*?'
    r'\bcall_id:\s*"(?P<call>call_[^"]+)"'
)
TOOL_DONE_RE = re.compile(
    rf':handle_tool_call_with_source:\s+tool call completed\b.*?'
    rf'\bturn_id=(?P<turn>{UUID_PATTERN})\b.*?'
    r'\bcall_id=(?P<call>call_[A-Za-z0-9_-]+)\b.*?'
    r'\bexecution_started=(?:true|false)\b'
)
REQUEST_RE = re.compile(r'(?:^|}:)\s*endpoint="/responses"(?:\s|$)')
MODEL_PREPARE_RE = re.compile(
    r':\s*model="[^"]+"\s+approval_policy=[^\s]+\s+sandbox_policy='
)
FIRST_STREAM_RE = re.compile(
    r':\s*unhandled responses event:\s*'
    r'(?:codex\.response\.metadata|response\.in_progress)\s*$'
)
ANY_STREAM_RE = re.compile(r':\s*unhandled responses event:\s*[^\r\n]+\s*$')
NORMAL_COMPLETE_RE = re.compile(
    rf':\s*post sampling token usage\b.*?\bturn_id=(?P<turn>{UUID_PATTERN})\b.*?'
    r'\bmodel_needs_follow_up=false\b.*?\bhas_pending_input=false\b.*?'
    r'\bneeds_follow_up=false\s*$'
)

TARGET_TOOL_START = "codex_core::stream_events_utils"
TARGET_TOOL_DONE = "codex_core::tools::parallel"
TARGET_REQUEST = "feedback_tags"
TARGET_STREAM = "codex_api::sse::responses"
TARGET_COMPLETE = "codex_core::session::turn"
TARGET_ABORT = "codex_core::tasks"
TARGETS = (
    TARGET_TOOL_START,
    TARGET_TOOL_DONE,
    TARGET_REQUEST,
    TARGET_STREAM,
    TARGET_COMPLETE,
    TARGET_ABORT,
)


def utc_now() -> float:
    return time.time()


def iso_time(timestamp: float | None = None) -> str:
    stamp = utc_now() if timestamp is None else timestamp
    return dt.datetime.fromtimestamp(stamp, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso_time(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def default_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def default_config(home: Path) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "enabled": False,
        "db_path": str(home / "logs_2.sqlite"),
        "poll_seconds": 5.0,
        "post_tool_seconds": 45.0,
        "response_seconds": 120.0,
        "critical_seconds": 180.0,
        "opaque_model_seconds": 600.0,
        "stream_silence_seconds": 900.0,
        "tool_warning_seconds": 180.0,
        "tool_critical_seconds": 600.0,
        "log_batch_rows": MAX_LOG_BATCH_ROWS,
        "reminder_seconds": 180.0,
        "notify": True,
        "task_name": DEFAULT_TASK_NAME,
    }


def read_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return json.loads(json.dumps(fallback))
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return data


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


class FileLock:
    """Small cross-process lock protecting watchdog JSON state."""

    def __init__(self, path: Path, timeout: float = 10.0):
        self.path = path
        self.timeout = timeout
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self.handle.close()
                    raise TimeoutError(f"Timed out acquiring {self.path}")
                time.sleep(0.05)

    def __exit__(self, exc_type, exc, tb):
        if self.handle is None:
            return
        self.handle.seek(0)
        if os.name == "nt":
            import msvcrt

            with contextlib.suppress(OSError):
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            with contextlib.suppress(OSError):
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()


class Runtime:
    def __init__(self, home: Path):
        self.home = home.resolve()
        self.root = self.home / "watchdog"
        self.config_path = self.root / "config.json"
        self.jobs_path = self.root / "jobs.json"
        self.state_path = self.root / "state.json"
        self.incidents_path = self.root / "incidents.jsonl"
        self.recovery_dir = self.root / "recovery_manifests"
        self.pid_path = self.root / "pid.json"
        self.error_path = self.root / "last_error.json"
        self.lock_path = self.root / "runtime.lock"
        self.root.mkdir(parents=True, exist_ok=True)
        self.ensure_files()

    def ensure_files(self) -> None:
        with FileLock(self.lock_path):
            if not self.config_path.exists():
                atomic_write_json(self.config_path, default_config(self.home))
            if not self.jobs_path.exists():
                atomic_write_json(
                    self.jobs_path,
                    {"schema_version": SCHEMA_VERSION, "updated_at": iso_time(), "jobs": []},
                )
            if not self.state_path.exists():
                atomic_write_json(
                    self.state_path,
                    {
                        "schema_version": SCHEMA_VERSION,
                        "detector_version": DETECTOR_VERSION,
                        "initialized": False,
                        "last_log_id": 0,
                        "database_identity": None,
                        "turns": {},
                        "updated_at": iso_time(),
                    },
                )

    def config(self) -> dict:
        merged = default_config(self.home)
        merged.update(read_json(self.config_path, merged))
        return merged

    def save_config(self, config: dict) -> None:
        config["schema_version"] = SCHEMA_VERSION
        atomic_write_json(self.config_path, config)

    def jobs(self) -> dict:
        return read_json(
            self.jobs_path,
            {"schema_version": SCHEMA_VERSION, "updated_at": iso_time(), "jobs": []},
        )

    def save_jobs(self, jobs: dict) -> None:
        jobs["schema_version"] = SCHEMA_VERSION
        jobs["updated_at"] = iso_time()
        atomic_write_json(self.jobs_path, jobs)

    def state(self) -> dict:
        return read_json(
            self.state_path,
            {
                "schema_version": SCHEMA_VERSION,
                "detector_version": DETECTOR_VERSION,
                "initialized": False,
                "last_log_id": 0,
                "database_identity": None,
                "turns": {},
                "updated_at": iso_time(),
            },
        )

    def save_state(self, state: dict) -> None:
        state["schema_version"] = SCHEMA_VERSION
        state["updated_at"] = iso_time()
        atomic_write_json(self.state_path, state)

    def append_incident(self, incident: dict) -> None:
        self.incidents_path.parent.mkdir(parents=True, exist_ok=True)
        encoded = (json.dumps(incident, ensure_ascii=False, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        current_size = self.incidents_path.stat().st_size if self.incidents_path.exists() else 0
        if current_size and current_size + len(encoded) > INCIDENT_MAX_BYTES:
            self.rotate_incidents()
        with self.incidents_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded.decode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())

    def incident_files(self) -> list[Path]:
        return [
            self.incidents_path.with_name(self.incidents_path.name + f".{index}")
            for index in range(1, INCIDENT_BACKUP_COUNT + 1)
        ]

    def rotate_incidents(self) -> bool:
        if not self.incidents_path.exists() or self.incidents_path.stat().st_size == 0:
            return False
        backups = self.incident_files()
        backups[-1].unlink(missing_ok=True)
        for source, destination in zip(reversed(backups[:-1]), reversed(backups[1:])):
            if source.exists():
                os.replace(source, destination)
        os.replace(self.incidents_path, backups[0])
        return True


def compact_body(body: str | None) -> str:
    text = body or ""
    if len(text) <= 131072:
        return text
    return text[:65536] + "\n...[truncated by watchdog]...\n" + text[-65536:]


def extract_turn_id(body: str) -> str | None:
    for pattern in (TURN_SCOPE_RE, SAMPLING_TURN_RE, EVENT_TURN_RE, PASSTHROUGH_TURN_RE):
        match = pattern.search(body)
        if match:
            return match.group(1)
    return None


def event_timestamp(seconds: int | float | None, nanos: int | None) -> float:
    value = float(seconds or 0)
    if nanos is not None and 0 <= int(nanos) < 1_000_000_000:
        value += int(nanos) / 1_000_000_000
    return value


def turn_key(process_uuid: str | None, thread_id: str, turn_id: str) -> str:
    return "|".join((process_uuid or "unknown-process", thread_id, turn_id))


def fresh_turn(process_uuid: str | None, thread_id: str, turn_id: str, stamp: float) -> dict:
    return {
        "process_uuid": process_uuid or "unknown-process",
        "thread_id": thread_id,
        "turn_id": turn_id,
        "phase": "observed",
        "active_calls": [],
        "active_call_started_at": {},
        "tool_incidents": {},
        "observed_tool_start": False,
        "post_tool_at": None,
        "model_preparing_at": None,
        "request_at": None,
        "request_attempt": 0,
        "first_stream_at": None,
        "last_stream_at": None,
        "terminal": None,
        "last_event_at": stamp,
        "incident": None,
        "recovery_manifest": None,
    }


def clear_incident(turn: dict) -> None:
    turn["incident"] = None


def clear_active_calls(turn: dict) -> None:
    turn["active_calls"] = []
    turn["active_call_started_at"] = {}
    turn["tool_incidents"] = {}


def process_event(state: dict, row: tuple) -> None:
    row_id, ts, ts_nanos, target, raw_body, thread_id, process_uuid = row
    if not thread_id:
        return
    body = compact_body(raw_body)
    stamp = event_timestamp(ts, ts_nanos)

    if target == TARGET_ABORT:
        match = ABORT_RE.search(body)
        if not match:
            return
        found_turn = match.group(1)
    elif target == TARGET_TOOL_DONE:
        match = TOOL_DONE_RE.search(body)
        if not match:
            return
        found_turn = match.group("turn")
    elif target == TARGET_COMPLETE:
        match = NORMAL_COMPLETE_RE.search(body)
        if not match:
            return
        found_turn = match.group("turn")
    else:
        found_turn = extract_turn_id(body)
        if not found_turn:
            return

    key = turn_key(process_uuid, thread_id, found_turn)
    turns = state.setdefault("turns", {})
    turn = turns.setdefault(key, fresh_turn(process_uuid, thread_id, found_turn, stamp))
    turn["last_event_at"] = stamp

    if turn.get("terminal"):
        return

    if target == TARGET_TOOL_START:
        match = TOOL_START_RE.search(body)
        if not match:
            return
        call_id = match.group("call")
        active = set(turn.get("active_calls", []))
        active.add(call_id)
        turn["active_calls"] = sorted(active)
        started = turn.setdefault("active_call_started_at", {})
        started.setdefault(call_id, stamp)
        turn.setdefault("tool_incidents", {}).pop(call_id, None)
        turn["observed_tool_start"] = True
        turn["phase"] = "tool_running"
        turn["post_tool_at"] = None
        turn["model_preparing_at"] = None
        turn["request_at"] = None
        clear_incident(turn)
        return

    if target == TARGET_TOOL_DONE:
        call_id = match.group("call")
        active = set(turn.get("active_calls", []))
        was_active = call_id in active
        active.discard(call_id)
        turn["active_calls"] = sorted(active)
        turn.setdefault("active_call_started_at", {}).pop(call_id, None)
        turn.setdefault("tool_incidents", {}).pop(call_id, None)
        if was_active and turn.get("observed_tool_start") and not active:
            turn["phase"] = "post_tool_pending"
            turn["post_tool_at"] = stamp
            turn["model_preparing_at"] = None
            turn["request_at"] = None
            turn["first_stream_at"] = None
            clear_incident(turn)
        return

    if target == TARGET_REQUEST:
        if REQUEST_RE.search(body):
            clear_active_calls(turn)
            turn["phase"] = "request_pending"
            turn["request_at"] = stamp
            turn["request_attempt"] = int(turn.get("request_attempt", 0)) + 1
            turn["post_tool_at"] = None
            turn["model_preparing_at"] = None
            turn["first_stream_at"] = None
            clear_incident(turn)
            return
        if MODEL_PREPARE_RE.search(body):
            clear_active_calls(turn)
            turn["phase"] = "model_preparing"
            turn["model_preparing_at"] = stamp
            turn["post_tool_at"] = None
            turn["request_at"] = None
            turn["first_stream_at"] = None
            clear_incident(turn)
            return

    if target == TARGET_STREAM and ANY_STREAM_RE.search(body):
        clear_active_calls(turn)
        turn["last_stream_at"] = stamp
        if FIRST_STREAM_RE.search(body) or turn.get("phase") != "streaming":
            turn["phase"] = "streaming"
            turn["first_stream_at"] = turn.get("first_stream_at") or stamp
            turn["request_at"] = None
            turn["model_preparing_at"] = None
            clear_incident(turn)
        return

    if target == TARGET_COMPLETE:
        turn["phase"] = "terminal"
        turn["terminal"] = "completed"
        clear_active_calls(turn)
        turn["post_tool_at"] = None
        turn["model_preparing_at"] = None
        turn["request_at"] = None
        clear_incident(turn)
        return

    if target == TARGET_ABORT:
        turn["phase"] = "terminal"
        turn["terminal"] = "aborted"
        clear_active_calls(turn)
        turn["post_tool_at"] = None
        turn["model_preparing_at"] = None
        turn["request_at"] = None
        clear_incident(turn)


def make_incident(
    kind: str,
    severity: str,
    age_seconds: float,
    *,
    thread_id: str,
    turn_id: str,
    tag: str | None = None,
    call_id: str | None = None,
    sequence: int = 1,
    evidence_class: str = "absence_only",
    observability: str = "opaque",
    confirmed_failure: bool = False,
    safe_to_interrupt: bool = False,
    recommended_action: str = "inspect_live_task",
) -> dict:
    incident = {
        "schema_version": SCHEMA_VERSION,
        "recorded_at": iso_time(),
        "kind": kind,
        "severity": severity,
        "age_seconds": round(age_seconds, 1),
        "thread_id": thread_id,
        "turn_id": turn_id,
        "tag": tag,
        "sequence": sequence,
        "automatic_action": "none",
        "evidence_class": evidence_class,
        "observability": observability,
        "confirmed_failure": confirmed_failure,
        "safe_to_interrupt": safe_to_interrupt,
        "recommended_action": recommended_action,
    }
    if call_id is not None:
        incident["call_id"] = call_id
    return incident


def incident_due(previous: dict | None, severity: str, now: float, reminder_seconds: float) -> bool:
    if not previous:
        return True
    if previous.get("severity") != severity:
        return True
    return now - float(previous.get("notified_at", 0)) >= reminder_seconds


def find_rollout_metadata(home: Path, thread_id: str) -> dict:
    """Locate rollout metadata without opening or parsing rollout contents."""
    if not re.fullmatch(UUID_PATTERN, thread_id or ""):
        return {"path": None, "size_bytes": None}
    candidates: list[tuple[int, Path]] = []
    for base_name in ("sessions", "archived_sessions"):
        base = home / base_name
        if not base.exists():
            continue
        try:
            for candidate in base.rglob(f"*{thread_id}*.jsonl"):
                try:
                    stat = candidate.stat()
                except OSError:
                    continue
                if candidate.is_file():
                    candidates.append((stat.st_mtime_ns, candidate.resolve()))
        except OSError:
            continue
    if not candidates:
        return {"path": None, "size_bytes": None}
    _, path = max(candidates, key=lambda item: item[0])
    try:
        size = path.stat().st_size
    except OSError:
        return {"path": str(path), "size_bytes": None}
    return {"path": str(path), "size_bytes": size}


def write_recovery_manifest(
    runtime: Runtime,
    incident: dict,
    *,
    turn: dict | None = None,
    job: dict | None = None,
) -> Path:
    now = utc_now()
    turn = turn or {}
    job = job or {}
    thread_id = str(incident.get("thread_id") or turn.get("thread_id") or job.get("thread_id"))
    turn_id = str(incident.get("turn_id") or turn.get("turn_id") or job.get("turn_id"))
    started = turn.get("active_call_started_at", {})
    active_calls = []
    for call_id in sorted(str(value) for value in turn.get("active_calls", [])):
        stamp = float(started.get(call_id, 0) or 0)
        active_calls.append(
            {
                "call_id": call_id,
                "started_at": iso_time(stamp) if stamp > 0 else None,
            }
        )
    event_times = {"manifest_created_at": iso_time(now)}
    for key in (
        "last_event_at",
        "post_tool_at",
        "model_preparing_at",
        "request_at",
        "first_stream_at",
        "last_stream_at",
    ):
        stamp = float(turn.get(key, 0) or 0)
        if stamp > 0:
            event_times[key] = iso_time(stamp)
    for key in ("armed_at_epoch", "last_progress_at", "last_incident_at"):
        stamp = float(job.get(key, 0) or 0)
        if stamp > 0:
            event_times[key] = iso_time(stamp)
    manifest = {
        "thread_id": thread_id,
        "turn_id": turn_id,
        "phase": turn.get("phase") or ("manual_job_stalled" if job else "unknown"),
        "active_calls": active_calls,
        "event_times": event_times,
        "rollout": find_rollout_metadata(runtime.home, thread_id),
        "incident": {
            "kind": incident.get("kind"),
            "severity": incident.get("severity"),
            "evidence_class": incident.get("evidence_class", "absence_only"),
            "confirmed_failure": bool(incident.get("confirmed_failure", False)),
            "safe_to_interrupt": bool(incident.get("safe_to_interrupt", False)),
            "recommended_action": incident.get(
                "recommended_action", "inspect_live_task"
            ),
        },
        "resume": {
            "strategy": "same_thread_first",
            "automatic_wake": False,
            "wake_requires_live_state_check": True,
            "fallback": "small_disk_handoff_then_clean_task",
        },
        "recommendations": [
            "Inspect the live task state and incomplete model output, not only completed command records.",
            "Treat absence-only evidence as review due; it never proves that the agent stopped editing.",
            "If the task is confirmed stopped and unfinished, wake the same task once before creating a handoff.",
            "Inspect actual tool outputs before retrying; a missing UI event is not proof of failure.",
            "Do not automatically replay side-effecting or quota-spending tools.",
            "If the rollout is very large, write a concise handoff and continue in a fresh task.",
        ],
    }
    runtime.recovery_dir.mkdir(parents=True, exist_ok=True)
    safe_thread = re.sub(r"[^A-Za-z0-9_-]", "_", thread_id)[:48] or "unknown-thread"
    safe_turn = re.sub(r"[^A-Za-z0-9_-]", "_", turn_id)[:48] or "unknown-turn"
    path = runtime.recovery_dir / (
        f"recovery-{int(now * 1000)}-{safe_thread}-{safe_turn}-{uuid.uuid4().hex[:8]}.json"
    )
    atomic_write_json(path, manifest)
    return path


def check_turn_incidents(runtime: Runtime, state: dict, config: dict, now: float) -> list[dict]:
    created: list[dict] = []
    post_limit = float(config["post_tool_seconds"])
    response_limit = float(config["response_seconds"])
    critical_limit = float(config["critical_seconds"])
    opaque_limit = float(config.get("opaque_model_seconds", 600.0))
    stream_limit = float(config.get("stream_silence_seconds", 900.0))
    tool_warning_limit = float(config.get("tool_warning_seconds", 180.0))
    tool_critical_limit = float(config.get("tool_critical_seconds", 600.0))
    reminder = float(config["reminder_seconds"])

    for turn in state.get("turns", {}).values():
        if turn.get("terminal"):
            continue
        if turn.get("phase") in ("request_pending", "streaming") and turn.get("active_calls"):
            clear_active_calls(turn)
        active_calls = sorted(str(call_id) for call_id in turn.get("active_calls", []))
        started = turn.setdefault("active_call_started_at", {})
        tool_previous = turn.setdefault("tool_incidents", {})
        turn_recovery_manifest = turn.get("recovery_manifest")
        for stale_call in set(started) - set(active_calls):
            started.pop(stale_call, None)
            tool_previous.pop(stale_call, None)
        for call_id in active_calls:
            anchor = float(started.get(call_id, 0) or 0)
            if anchor <= 0:
                anchor = float(turn.get("last_event_at", now) or now)
                started[call_id] = anchor
            age = now - anchor
            if age < tool_warning_limit:
                continue
            severity = "review" if age >= tool_critical_limit else "warning"
            previous = tool_previous.get(call_id)
            if not incident_due(previous, severity, now, reminder):
                continue
            sequence = int(previous.get("sequence", 0)) + 1 if previous else 1
            incident = make_incident(
                "tool_running_no_completion",
                severity,
                age,
                thread_id=turn["thread_id"],
                turn_id=turn["turn_id"],
                call_id=call_id,
                sequence=sequence,
                observability="tool_call_without_completion_event",
                recommended_action="inspect_tool_and_live_task",
            )
            recovery_manifest = previous.get("recovery_manifest") if previous else None
            if severity == "review":
                recovery_manifest = recovery_manifest or turn_recovery_manifest
                if not recovery_manifest or not Path(recovery_manifest).is_file():
                    recovery_manifest = str(
                        write_recovery_manifest(runtime, incident, turn=turn)
                    )
                turn["recovery_manifest"] = recovery_manifest
                turn_recovery_manifest = recovery_manifest
                incident["recovery_manifest"] = recovery_manifest
            runtime.append_incident(incident)
            tool_previous[call_id] = {
                "severity": severity,
                "sequence": sequence,
                "notified_at": now,
                "recovery_manifest": recovery_manifest,
            }
            created.append(incident)
        phase = turn.get("phase")
        if phase == "post_tool_pending" and turn.get("post_tool_at") is not None:
            anchor = float(turn["post_tool_at"])
            limit = max(post_limit, opaque_limit)
            kind = "post_tool_transition_unobserved"
            observability = "post_tool_model_work_unobserved"
            recommended_action = "inspect_live_task_without_interrupting"
        elif phase == "model_preparing" and turn.get("model_preparing_at") is not None:
            anchor = float(turn["model_preparing_at"])
            limit = opaque_limit
            kind = "model_preparing_no_request"
            observability = "opaque_model_preparation"
            recommended_action = "inspect_live_task_without_interrupting"
        elif phase == "request_pending" and turn.get("request_at") is not None:
            anchor = float(turn["request_at"])
            limit = response_limit
            kind = "request_no_first_event"
            observability = "network_request_waiting_for_first_event"
            recommended_action = "inspect_transport_and_live_task"
        elif phase == "streaming" and turn.get("last_stream_at") is not None:
            anchor = float(turn["last_stream_at"])
            limit = stream_limit
            kind = "stream_no_recent_event"
            observability = "opaque_model_stream"
            recommended_action = "inspect_live_stream_without_interrupting"
        else:
            continue
        age = now - anchor
        if age < limit:
            continue
        severity = "review" if age >= max(critical_limit, limit) else "warning"
        previous = turn.get("incident")
        if not incident_due(previous, severity, now, reminder):
            continue
        sequence = int(previous.get("sequence", 0)) + 1 if previous else 1
        incident = make_incident(
            kind,
            severity,
            age,
            thread_id=turn["thread_id"],
            turn_id=turn["turn_id"],
            sequence=sequence,
            observability=observability,
            recommended_action=recommended_action,
        )
        recovery_manifest = previous.get("recovery_manifest") if previous else None
        if severity == "review":
            recovery_manifest = recovery_manifest or turn.get("recovery_manifest")
            if not recovery_manifest or not Path(recovery_manifest).is_file():
                recovery_manifest = str(write_recovery_manifest(runtime, incident, turn=turn))
            turn["recovery_manifest"] = recovery_manifest
            incident["recovery_manifest"] = recovery_manifest
        runtime.append_incident(incident)
        turn["incident"] = {
            "kind": kind,
            "severity": severity,
            "sequence": sequence,
            "notified_at": now,
            "recovery_manifest": recovery_manifest,
        }
        created.append(incident)
    return created


def check_manual_jobs(runtime: Runtime, jobs_doc: dict, config: dict, now: float) -> list[dict]:
    created: list[dict] = []
    reminder = float(config["reminder_seconds"])
    for job in jobs_doc.get("jobs", []):
        if job.get("state") not in ("armed", "stalled"):
            continue
        anchor = float(job.get("last_progress_at", job.get("armed_at_epoch", now)))
        age = now - anchor
        timeout = float(job.get("timeout_seconds", config["critical_seconds"]))
        if age < timeout:
            continue
        previous_at = float(job.get("last_incident_at", 0))
        if job.get("incident_count", 0) and now - previous_at < reminder:
            continue
        job["state"] = "stalled"
        job["incident_count"] = int(job.get("incident_count", 0)) + 1
        job["last_incident_at"] = now
        incident = make_incident(
            "armed_job_no_verified_progress",
            "review",
            age,
            thread_id=job["thread_id"],
            turn_id=job["turn_id"],
            tag=job["tag"],
            sequence=job["incident_count"],
            observability="manual_heartbeat_gap",
            recommended_action="inspect_live_task_then_heartbeat_or_recover",
        )
        if job["incident_count"] == 1:
            incident["recovery_manifest"] = str(
                write_recovery_manifest(runtime, incident, job=job)
            )
        runtime.append_incident(incident)
        created.append(incident)
    return created


def prune_jobs_doc(jobs_doc: dict, now: float) -> dict:
    jobs = list(jobs_doc.get("jobs", []))
    closed_candidates: list[tuple[float, int]] = []
    active_indexes: set[int] = set()
    cutoff = now - DISARMED_RETENTION_SECONDS
    for index, job in enumerate(jobs):
        if job.get("state") != "disarmed":
            active_indexes.add(index)
            continue
        disarmed_epoch = parse_iso_time(job.get("disarmed_at"))
        if disarmed_epoch >= cutoff:
            closed_candidates.append((disarmed_epoch, index))
    closed_candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    kept_closed = {index for _, index in closed_candidates[:MAX_DISARMED_JOBS]}
    keep_indexes = active_indexes | kept_closed
    retained = [job for index, job in enumerate(jobs) if index in keep_indexes]
    removed = len(jobs) - len(retained)
    if removed:
        jobs_doc["jobs"] = retained
    return {
        "removed": removed,
        "retained": len(retained),
        "active_preserved": len(active_indexes),
        "closed_retained": len(kept_closed),
    }


def ensure_cleanup_scope(runtime: Runtime) -> Path:
    expected = runtime.home.resolve() / "watchdog"
    actual = runtime.root.resolve()
    if actual != expected:
        raise ValueError(f"Refusing cleanup outside {expected}: {actual}")
    return actual


def cleanup_runtime(runtime: Runtime, apply: bool = False) -> dict:
    scope = ensure_cleanup_scope(runtime)
    now = utc_now()
    with FileLock(runtime.lock_path):
        jobs_doc = runtime.jobs()
        jobs_plan = prune_jobs_doc(jobs_doc, now)
        incident_size = (
            runtime.incidents_path.stat().st_size if runtime.incidents_path.exists() else 0
        )
        would_rotate = incident_size > INCIDENT_MAX_BYTES
        if apply and jobs_plan["removed"]:
            runtime.save_jobs(jobs_doc)
        rotated = runtime.rotate_incidents() if apply and would_rotate else False
    return {
        "scope": str(scope),
        "mode": "apply" if apply else "dry-run",
        "jobs": jobs_plan,
        "incidents": {
            "active_size_bytes": incident_size,
            "rotation_threshold_bytes": INCIDENT_MAX_BYTES,
            "would_rotate": would_rotate,
            "rotated": rotated,
            "backup_limit": INCIDENT_BACKUP_COUNT,
        },
        "codex_data_touched": False,
    }


def prune_turns(state: dict, now: float) -> None:
    turns = state.get("turns", {})
    removable = []
    for key, turn in turns.items():
        if turn.get("active_calls"):
            continue
        age = now - float(turn.get("last_event_at", now))
        if turn.get("terminal") and age > 3600:
            removable.append(key)
        elif age > 86400:
            removable.append(key)
    for key in removable:
        turns.pop(key, None)
    if len(turns) > 250:
        ordered = sorted(
            (key for key in turns if not turns[key].get("active_calls")),
            key=lambda key: turns[key].get("last_event_at", 0),
        )
        for key in ordered[: max(0, len(turns) - 250)]:
            turns.pop(key, None)


def open_logs(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=0.5)
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=500")
    return connection


def poll_logs(runtime: Runtime, state: dict, config: dict) -> dict:
    database = Path(config["db_path"]).expanduser()
    if not database.exists():
        return {"database": "missing", "rows": 0}
    stat = database.stat()
    identity = f"{stat.st_dev}:{stat.st_ino}"
    with contextlib.closing(open_logs(database)) as connection:
        maximum = int(connection.execute("SELECT COALESCE(MAX(id), 0) FROM logs").fetchone()[0])
        if not state.get("initialized"):
            state["initialized"] = True
            state["last_log_id"] = maximum
            state["database_identity"] = identity
            return {"database": "initialized_from_tail", "rows": 0}
        cursor = int(state.get("last_log_id", 0))
        previous_identity = state.get("database_identity")
        if (previous_identity and previous_identity != identity) or maximum < cursor:
            state["turns"] = {}
            state["last_log_id"] = maximum
            state["database_identity"] = identity
            state["database_reset_at"] = iso_time()
            return {"database": "reset_from_tail", "rows": 0}
        state["database_identity"] = identity
        if maximum == cursor:
            return {"database": "ok", "rows": 0}
        placeholders = ",".join("?" for _ in TARGETS)
        batch_limit = min(
            MAX_LOG_BATCH_ROWS,
            max(1, int(config.get("log_batch_rows", MAX_LOG_BATCH_ROWS))),
        )
        query = (
            "SELECT id,ts,ts_nanos,target,feedback_log_body,thread_id,process_uuid "
            f"FROM logs WHERE id>? AND id<=? AND target IN ({placeholders}) "
            "ORDER BY id LIMIT ?"
        )
        row_cursor = connection.execute(
            query,
            (cursor, maximum, *TARGETS, batch_limit),
        )
        row_count = 0
        last_matching_id = cursor
        for row in row_cursor:
            process_event(state, row)
            row_count += 1
            last_matching_id = int(row[0])
        if row_count < batch_limit:
            state["last_log_id"] = maximum
        else:
            state["last_log_id"] = last_matching_id
        return {
            "database": "ok",
            "rows": row_count,
            "backlog": bool(row_count == batch_limit and last_matching_id < maximum),
        }


def notify_windows(title: str, message: str) -> bool:
    if os.name != "nt":
        return False
    powershell = r"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$n = New-Object System.Windows.Forms.NotifyIcon
$n.Icon = [System.Drawing.SystemIcons]::Warning
$n.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Warning
$n.BalloonTipTitle = $env:CODEX_WD_TITLE
$n.BalloonTipText = $env:CODEX_WD_TEXT
$n.Visible = $true
$n.ShowBalloonTip(10000)
Start-Sleep -Seconds 12
$n.Dispose()
"""
    environment = os.environ.copy()
    environment["CODEX_WD_TITLE"] = title[:63]
    environment["CODEX_WD_TEXT"] = message[:255]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-Command", powershell],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            creationflags=flags,
            close_fds=True,
        )
        return True
    except OSError:
        return False


def notify_incidents(config: dict, incidents: list[dict]) -> None:
    if not config.get("notify", True):
        return
    grouped: dict[tuple[str, str, str, str], list[dict]] = {}
    for incident in incidents:
        key = (
            str(incident.get("thread_id", "")),
            str(incident.get("turn_id", "")),
            str(incident.get("kind", "unknown")),
            str(incident.get("severity", "warning")),
        )
        grouped.setdefault(key, []).append(incident)
    for (thread_id, turn_id, kind, severity), records in grouped.items():
        thread = thread_id[:8]
        turn = turn_id[:8]
        age = max(float(record.get("age_seconds", 0)) for record in records)
        count = len(records)
        subject = f"{count} calls" if count > 1 else "1 event"
        notify_windows(
            "Codex watchdog review",
            f"{severity} {kind}: {subject}, thread {thread}, turn {turn}, gap {age:.1f}s. Agent may still be working; do not interrupt from this alert alone.",
        )


def run_once(runtime: Runtime) -> dict:
    now = utc_now()
    notifications: list[dict] = []
    with FileLock(runtime.lock_path):
        config = runtime.config()
        if not config.get("enabled"):
            return {"enabled": False, "incidents": 0, "rows": 0}
        state = runtime.state()
        jobs_doc = runtime.jobs()
        state_before = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if int(state.get("detector_version", 0)) != DETECTOR_VERSION:
            state["turns"] = {}
            state["detector_version"] = DETECTOR_VERSION
            state["detector_migrated_at"] = iso_time(now)
        jobs_before = json.dumps(jobs_doc, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        poll_result = poll_logs(runtime, state, config)
        notifications.extend(check_turn_incidents(runtime, state, config, now))
        notifications.extend(check_manual_jobs(runtime, jobs_doc, config, now))
        prune_jobs_doc(jobs_doc, now)
        prune_turns(state, now)
        if json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":")) != state_before:
            runtime.save_state(state)
        if json.dumps(jobs_doc, ensure_ascii=False, sort_keys=True, separators=(",", ":")) != jobs_before:
            runtime.save_jobs(jobs_doc)
    notify_incidents(config, notifications)
    return {
        "enabled": True,
        "incidents": len(notifications),
        "rows": poll_result["rows"],
        "database": poll_result["database"],
    }


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def pid_status(runtime: Runtime) -> dict:
    data = read_json(runtime.pid_path, {}) if runtime.pid_path.exists() else {}
    pid = int(data.get("pid", 0) or 0)
    return {"running": process_alive(pid), "pid": pid or None, "started_at": data.get("started_at")}


def write_pid(runtime: Runtime) -> None:
    atomic_write_json(
        runtime.pid_path,
        {"pid": os.getpid(), "started_at": iso_time(), "script": str(Path(__file__).resolve())},
    )


def clear_pid(runtime: Runtime) -> None:
    if not runtime.pid_path.exists():
        return
    with contextlib.suppress(Exception):
        data = read_json(runtime.pid_path, {})
        if int(data.get("pid", 0)) == os.getpid():
            runtime.pid_path.unlink(missing_ok=True)


def record_daemon_error(runtime: Runtime, error: Exception) -> None:
    now = utc_now()
    signature = f"{type(error).__name__}:{str(error)[:500]}"
    previous = read_json(runtime.error_path, {}) if runtime.error_path.exists() else {}
    if previous.get("signature") == signature and now - float(previous.get("recorded_at_epoch", 0)) < 300:
        return
    atomic_write_json(
        runtime.error_path,
        {
            "recorded_at": iso_time(now),
            "recorded_at_epoch": now,
            "signature": signature,
            "automatic_action": "none; daemon will retry polling",
        },
    )


def spawn_daemon(runtime: Runtime) -> dict:
    config = runtime.config()
    if not config.get("enabled"):
        return {"started": False, "reason": "disabled"}
    current = pid_status(runtime)
    if current["running"]:
        return {"started": False, "reason": "already_running", **current}
    executable = Path(sys.executable)
    if os.name == "nt":
        pythonw = executable.with_name("pythonw.exe")
        if pythonw.exists():
            executable = pythonw
    command = [
        str(executable),
        str(Path(__file__).resolve()),
        "--home",
        str(runtime.home),
        "run",
    ]
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
        close_fds=True,
    )
    deadline = time.monotonic() + 3.0
    status = {"running": False, "pid": process.pid, "started_at": None}
    while time.monotonic() < deadline:
        time.sleep(0.1)
        status = pid_status(runtime)
        if status["running"]:
            break
    won_start = status.get("pid") == process.pid and status.get("running")
    result = {"started": bool(won_start), **status}
    if not won_start:
        result["reason"] = "another_start_won"
    return result


def run_daemon(runtime: Runtime, once: bool = False) -> int:
    if once:
        print(json.dumps(run_once(runtime), ensure_ascii=False, sort_keys=True))
        return 0
    with FileLock(runtime.lock_path):
        config = runtime.config()
        if not config.get("enabled"):
            return 0
        current = pid_status(runtime)
        if current["running"] and current.get("pid") != os.getpid():
            return 0
        write_pid(runtime)
    try:
        while True:
            delay = 5.0
            try:
                config = runtime.config()
                if not config.get("enabled"):
                    break
                delay = max(1.0, float(config.get("poll_seconds", 5.0)))
                run_once(runtime)
            except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError) as error:
                record_daemon_error(runtime, error)
            time.sleep(delay)
    except KeyboardInterrupt:
        pass
    finally:
        clear_pid(runtime)
    return 0


def infer_turn(runtime: Runtime, thread_id: str) -> str | None:
    config = runtime.config()
    database = Path(config["db_path"]).expanduser()
    if not database.exists() or not thread_id or thread_id.startswith("unknown"):
        return None
    try:
        with contextlib.closing(open_logs(database)) as connection:
            rows = connection.execute(
                "SELECT target,feedback_log_body FROM logs WHERE thread_id=? "
                "AND target IN (?,?,?,?,?,?) ORDER BY id DESC LIMIT 250",
                (thread_id, *TARGETS),
            )
            for target, raw_body in rows:
                body = compact_body(raw_body)
                if target == TARGET_ABORT:
                    match = ABORT_RE.search(body)
                    if match:
                        return match.group(1)
                found = extract_turn_id(body)
                if found:
                    return found
    except (OSError, sqlite3.Error):
        return None
    return None


def create_tag(thread_id: str, turn_id: str, kind: str, generation: int) -> str:
    quote = lambda value: urllib.parse.quote(str(value), safe="")
    return (
        "[CODEX-WATCHDOG|"
        f"thread={quote(thread_id)}|turn={quote(turn_id)}|kind={quote(kind)}|"
        f"uuid={uuid.uuid4()}|generation={int(generation)}]"
    )


def arm_job(runtime: Runtime, args) -> dict:
    with FileLock(runtime.lock_path):
        config = runtime.config()
        if not config.get("enabled"):
            return {"armed": False, "reason": "disabled"}
        thread_id = args.thread or os.environ.get("CODEX_THREAD_ID") or "unknown-thread"
        turn_id = args.turn
        if not turn_id or turn_id == "auto":
            turn_id = infer_turn(runtime, thread_id) or f"logical-{int(utc_now())}"
        tag = create_tag(thread_id, turn_id, args.kind, args.generation)
        now = utc_now()
        jobs_doc = runtime.jobs()
        prune_jobs_doc(jobs_doc, now)
        jobs_doc.setdefault("jobs", []).append(
            {
                "tag": tag,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "kind": args.kind,
                "uuid": tag.split("uuid=", 1)[1].split("|", 1)[0],
                "generation": int(args.generation),
                "label": args.label,
                "state": "armed",
                "armed_at": iso_time(now),
                "armed_at_epoch": now,
                "last_heartbeat_at": iso_time(now),
                "last_progress_at": now,
                "timeout_seconds": float(args.timeout_seconds),
                "incident_count": 0,
                "last_incident_at": 0,
                "note": "armed",
                "disarmed_at": None,
                "reason": None,
            }
        )
        runtime.save_jobs(jobs_doc)
    return {"armed": True, "tag": tag, "thread_id": thread_id, "turn_id": turn_id}


def update_job(runtime: Runtime, tag: str, action: str, note: str | None) -> dict:
    with FileLock(runtime.lock_path):
        jobs_doc = runtime.jobs()
        matching = [job for job in jobs_doc.get("jobs", []) if job.get("tag") == tag]
        if len(matching) != 1:
            return {"updated": False, "reason": "tag_not_found", "matches": len(matching)}
        job = matching[0]
        now = utc_now()
        if action == "heartbeat":
            if job.get("state") == "disarmed":
                return {"updated": False, "reason": "already_disarmed", "tag": tag}
            job["state"] = "armed"
            job["last_heartbeat_at"] = iso_time(now)
            job["last_progress_at"] = now
            job["note"] = note or "verified progress"
        elif action == "disarm":
            if job.get("state") != "disarmed":
                job["state"] = "disarmed"
                job["disarmed_at"] = iso_time(now)
                job["reason"] = note or "completed"
        prune_jobs_doc(jobs_doc, now)
        runtime.save_jobs(jobs_doc)
        return {"updated": True, "action": action, "tag": tag, "state": job["state"]}


def read_incidents(runtime: Runtime, limit: int, include_all: bool) -> list[dict]:
    requested = MAX_OUTPUT_RECORDS if include_all else min(MAX_OUTPUT_RECORDS, max(1, limit))
    records = deque(maxlen=requested)
    paths = list(reversed(runtime.incident_files())) + [runtime.incidents_path]
    for path in paths:
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return list(records)


def incident_for_recovery_review(record: dict | None) -> dict | None:
    if record is None:
        return None
    reviewed = dict(record)
    if "evidence_class" not in reviewed:
        reviewed["legacy_record"] = True
        reviewed["original_severity"] = reviewed.get("severity")
        reviewed["severity"] = "review"
        reviewed["evidence_class"] = "absence_only"
        reviewed["confirmed_failure"] = False
        reviewed["safe_to_interrupt"] = False
        reviewed["recommended_action"] = "inspect_live_task_without_interrupting"
    return reviewed


def build_recovery_plan(runtime: Runtime, thread_id: str, turn_id: str | None) -> dict:
    """Build a bounded, read-only recovery decision; never wake or stop a task."""
    state = runtime.state()
    candidates = [
        turn
        for turn in state.get("turns", {}).values()
        if str(turn.get("thread_id")) == thread_id
        and (turn_id is None or str(turn.get("turn_id")) == turn_id)
    ]
    turn = max(candidates, key=lambda item: float(item.get("last_event_at", 0)), default=None)
    selected_turn_id = turn_id or (str(turn.get("turn_id")) if turn else None)
    incidents = [
        record
        for record in read_incidents(runtime, MAX_OUTPUT_RECORDS, True)
        if str(record.get("thread_id")) == thread_id
        and (selected_turn_id is None or str(record.get("turn_id")) == selected_turn_id)
    ]
    latest_incident = incident_for_recovery_review(incidents[-1] if incidents else None)
    jobs = [
        job
        for job in runtime.jobs().get("jobs", [])
        if str(job.get("thread_id")) == thread_id
        and (selected_turn_id is None or str(job.get("turn_id")) == selected_turn_id)
        and job.get("state") != "disarmed"
    ]

    phase = str(turn.get("phase")) if turn else "unobserved"
    terminal = turn.get("terminal") if turn else None
    active_calls = list(turn.get("active_calls", [])) if turn else []
    if terminal == "completed":
        decision = "completed_no_recovery"
        live_state = "terminal_completed"
    elif terminal == "aborted":
        decision = "same_thread_wake_candidate"
        live_state = "terminal_aborted"
    elif active_calls or phase in {
        "tool_running",
        "model_preparing",
        "request_pending",
        "streaming",
    }:
        decision = "observe_no_interruption"
        live_state = "working_or_opaque"
    else:
        decision = "inspect_live_task_before_wake"
        live_state = "unconfirmed"

    return {
        "thread_id": thread_id,
        "turn_id": selected_turn_id,
        "phase": phase,
        "terminal": terminal,
        "active_calls": active_calls,
        "active_manual_tags": [job.get("tag") for job in jobs],
        "latest_incident": latest_incident,
        "decision": decision,
        "live_state": live_state,
        "safe_to_interrupt": False,
        "automatic_wake": False,
        "same_thread_first": True,
        "wake_gate": "live task is terminal/idle, unfinished, and has no advancing output",
        "next_steps": [
            "Inspect the target task's live state, including incomplete model output; completed command logs alone are insufficient.",
            "If it is still working or output is advancing, leave it untouched and heartbeat the exact watchdog tag.",
            "If it is confirmed stopped and unfinished, send one concise continuation to the same task; preserve its context and disk state.",
            "After reconnect, verify real outputs before retrying any missing side effect.",
            "Use a small disk handoff and a clean task only if same-task recovery is impossible or thread health is critical.",
        ],
    }


def bounded_jobs(jobs: list[dict], include_all: bool, limit: int) -> tuple[list[dict], bool]:
    selected = jobs if include_all else [job for job in jobs if job.get("state") != "disarmed"]
    requested = MAX_OUTPUT_RECORDS if include_all else min(MAX_OUTPUT_RECORDS, max(1, limit))
    truncated = len(selected) > requested
    return selected[-requested:], truncated


def startup_entry_installed(name: str) -> bool | None:
    if os.name != "nt":
        return None
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_READ,
        ) as key:
            winreg.QueryValueEx(key, name)
        return True
    except FileNotFoundError:
        return False


def startup_entry(
    runtime: Runtime,
    name: str,
    install: bool,
    dry_run: bool,
    expected_command: str | None = None,
) -> dict:
    if os.name != "nt":
        return {"changed": False, "reason": "windows_only"}
    import winreg

    executable = Path(sys.executable)
    pythonw = executable.with_name("pythonw.exe")
    if pythonw.exists():
        executable = pythonw
    command = subprocess.list2cmdline(
        [
            str(executable),
            str(Path(__file__).resolve()),
            "--home",
            str(runtime.home),
            "run",
        ]
    )
    result = {
        "method": "hkcu_run",
        "value_name": name,
        "command": command,
        "dry_run": dry_run,
    }
    if dry_run:
        result["changed"] = False
        return result
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Run",
        0,
        winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE,
    ) as key:
        try:
            existing, _ = winreg.QueryValueEx(key, name)
        except FileNotFoundError:
            existing = None
        if install:
            if existing is not None and existing != command:
                result.update(
                    {
                        "changed": False,
                        "reason": "value_name_conflict",
                        "existing_value_preserved": True,
                    }
                )
                return result
            if existing == command:
                result.update({"changed": False, "reason": "already_installed"})
                return result
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, command)
        else:
            if existing is None:
                result.update({"changed": False, "reason": "not_installed"})
                return result
            owned_command = expected_command or command
            if existing != owned_command:
                result.update(
                    {
                        "changed": False,
                        "reason": "value_name_conflict",
                        "existing_value_preserved": True,
                    }
                )
                return result
            winreg.DeleteValue(key, name)
    result["changed"] = True
    return result


def emit(data, json_output: bool) -> None:
    if json_output or isinstance(data, (dict, list)):
        print(json.dumps(data, ensure_ascii=False, indent=None if json_output else 2, sort_keys=True))
    else:
        print(data)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=default_home())
    parser.add_argument("--json", action="store_true", help="Emit compact JSON")
    subparsers = parser.add_subparsers(dest="command", required=True)

    enable = subparsers.add_parser("enable", help="Persistently enable and start the watchdog")
    enable.add_argument("--db", type=Path)
    enable.add_argument("--poll-seconds", type=float)
    enable.add_argument("--post-tool-seconds", type=float)
    enable.add_argument("--response-seconds", type=float)
    enable.add_argument("--critical-seconds", type=float)
    enable.add_argument("--opaque-model-seconds", type=float)
    enable.add_argument("--stream-silence-seconds", type=float)
    enable.add_argument("--tool-warning-seconds", type=float)
    enable.add_argument("--tool-critical-seconds", type=float)
    enable.add_argument("--log-batch-rows", type=int)
    enable.add_argument("--no-notify", action="store_true")
    enable.add_argument("--no-start", action="store_true")
    subparsers.add_parser("disable", help="Persistently disable; daemon exits on its next poll")
    subparsers.add_parser("start", help="Start a hidden daemon when enabled")
    run = subparsers.add_parser("run", help="Run the watchdog")
    run.add_argument("--once", action="store_true")
    subparsers.add_parser("status", help="Show configuration and runtime status")

    arm = subparsers.add_parser("arm", help="Register a long-running attempt")
    arm.add_argument("--thread")
    arm.add_argument("--turn", default="auto")
    arm.add_argument("--kind", required=True)
    arm.add_argument(
        "--timeout-seconds",
        type=float,
        default=300.0,
        help="Rolling no-progress threshold selected for this task class (default: 300)",
    )
    arm.add_argument("--label")
    arm.add_argument("--generation", type=int, default=1)

    heartbeat = subparsers.add_parser("heartbeat", help="Record verified progress for one tag")
    heartbeat.add_argument("tag")
    heartbeat.add_argument("--note")
    disarm = subparsers.add_parser("disarm", help="Close one exact tag")
    disarm.add_argument("tag")
    disarm.add_argument("--reason")
    listing = subparsers.add_parser("list", help="List watchdog jobs")
    listing.add_argument("--limit", type=int, default=DEFAULT_LIST_LIMIT)
    listing.add_argument("--all", action="store_true")
    incidents = subparsers.add_parser("incidents", help="List recorded incidents")
    incidents.add_argument("--limit", type=int, default=DEFAULT_INCIDENT_LIMIT)
    incidents.add_argument("--all", action="store_true")
    recovery = subparsers.add_parser(
        "recover-plan",
        help="Build a bounded same-task-first recovery plan without waking or stopping anything",
    )
    recovery.add_argument("--thread", required=True)
    recovery.add_argument("--turn")
    cleanup = subparsers.add_parser(
        "cleanup", help="Prune bounded watchdog metadata only; never Codex task data"
    )
    cleanup_mode = cleanup.add_mutually_exclusive_group()
    cleanup_mode.add_argument("--dry-run", action="store_true")
    cleanup_mode.add_argument("--apply", action="store_true")
    test_notify = subparsers.add_parser("test-notify", help="Send a harmless test notification")
    test_notify.add_argument("--message", default="Codex watchdog notification test")
    install = subparsers.add_parser("install", help="Install a per-user logon startup entry")
    install.add_argument("--task-name")
    install.add_argument("--dry-run", action="store_true")
    uninstall = subparsers.add_parser("uninstall", help="Remove the per-user startup entry")
    uninstall.add_argument("--task-name")
    uninstall.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    runtime = Runtime(args.home)

    if args.command == "enable":
        with FileLock(runtime.lock_path):
            config = runtime.config()
            config["enabled"] = True
            if args.db:
                config["db_path"] = str(args.db.resolve())
            for argument, key in (
                (args.poll_seconds, "poll_seconds"),
                (args.post_tool_seconds, "post_tool_seconds"),
                (args.response_seconds, "response_seconds"),
                (args.critical_seconds, "critical_seconds"),
                (args.opaque_model_seconds, "opaque_model_seconds"),
                (args.stream_silence_seconds, "stream_silence_seconds"),
                (args.tool_warning_seconds, "tool_warning_seconds"),
                (args.tool_critical_seconds, "tool_critical_seconds"),
            ):
                if argument is not None:
                    if argument <= 0:
                        parser.error(f"--{key.replace('_', '-')} must be positive")
                    config[key] = float(argument)
            if args.log_batch_rows is not None:
                if not 1 <= args.log_batch_rows <= MAX_LOG_BATCH_ROWS:
                    parser.error(f"--log-batch-rows must be between 1 and {MAX_LOG_BATCH_ROWS}")
                config["log_batch_rows"] = int(args.log_batch_rows)
            if float(config["tool_critical_seconds"]) < float(config["tool_warning_seconds"]):
                parser.error("--tool-critical-seconds must be >= --tool-warning-seconds")
            if args.no_notify:
                config["notify"] = False
            runtime.save_config(config)
        result = {"enabled": True, "started": None}
        if not args.no_start:
            result["started"] = spawn_daemon(runtime)
        emit(result, args.json)
        return 0

    if args.command == "disable":
        with FileLock(runtime.lock_path):
            config = runtime.config()
            config["enabled"] = False
            runtime.save_config(config)
        before = pid_status(runtime)
        deadline = time.monotonic() + max(2.0, float(config.get("poll_seconds", 5.0)) + 2.0)
        while before["running"] and time.monotonic() < deadline:
            time.sleep(0.2)
            before = pid_status(runtime)
        emit({"enabled": False, "daemon": before}, args.json)
        return 0

    if args.command == "start":
        emit(spawn_daemon(runtime), args.json)
        return 0

    if args.command == "run":
        return run_daemon(runtime, once=args.once)

    if args.command == "status":
        with FileLock(runtime.lock_path):
            config = runtime.config()
            jobs_doc = runtime.jobs()
            active = [job for job in jobs_doc.get("jobs", []) if job.get("state") != "disarmed"]
        emit(
            {
                "enabled": bool(config.get("enabled")),
                "daemon": pid_status(runtime),
                "active_jobs": len(active),
                "total_jobs": len(jobs_doc.get("jobs", [])),
                "db_path": config.get("db_path"),
                "poll_seconds": config.get("poll_seconds"),
                "post_tool_seconds": config.get("post_tool_seconds"),
                "response_seconds": config.get("response_seconds"),
                "critical_seconds": config.get("critical_seconds"),
                "opaque_model_seconds": config.get("opaque_model_seconds"),
                "stream_silence_seconds": config.get("stream_silence_seconds"),
                "tool_warning_seconds": config.get("tool_warning_seconds"),
                "tool_critical_seconds": config.get("tool_critical_seconds"),
                "log_batch_rows": min(
                    MAX_LOG_BATCH_ROWS,
                    max(1, int(config.get("log_batch_rows", MAX_LOG_BATCH_ROWS))),
                ),
                "notify": bool(config.get("notify")),
                "startup_entry": startup_entry_installed(config.get("task_name", DEFAULT_TASK_NAME)),
                "runtime_dir": str(runtime.root),
                "detector_version": DETECTOR_VERSION,
            },
            args.json,
        )
        return 0

    if args.command == "arm":
        if args.timeout_seconds <= 0:
            parser.error("--timeout-seconds must be positive")
        if args.generation < 0:
            parser.error("--generation must be non-negative")
        emit(arm_job(runtime, args), args.json)
        return 0

    if args.command == "heartbeat":
        emit(update_job(runtime, args.tag, "heartbeat", args.note), args.json)
        return 0

    if args.command == "disarm":
        emit(update_job(runtime, args.tag, "disarm", args.reason), args.json)
        return 0

    if args.command == "list":
        if args.limit <= 0:
            parser.error("--limit must be positive")
        with FileLock(runtime.lock_path):
            jobs = runtime.jobs().get("jobs", [])
        jobs, truncated = bounded_jobs(jobs, args.all, args.limit)
        if args.all:
            print(
                f"watchdog: --all is compatibility-only and capped at {MAX_OUTPUT_RECORDS} records",
                file=sys.stderr,
            )
        elif truncated:
            print(
                f"watchdog: output truncated; use --limit up to {MAX_OUTPUT_RECORDS}",
                file=sys.stderr,
            )
        emit(jobs, args.json)
        return 0

    if args.command == "incidents":
        if args.limit <= 0:
            parser.error("--limit must be positive")
        if args.all:
            print(
                f"watchdog: --all is compatibility-only and capped at {MAX_OUTPUT_RECORDS} records",
                file=sys.stderr,
            )
        with FileLock(runtime.lock_path):
            records = read_incidents(runtime, args.limit, args.all)
        emit(records, args.json)
        return 0

    if args.command == "recover-plan":
        with FileLock(runtime.lock_path):
            plan = build_recovery_plan(runtime, args.thread, args.turn)
        emit(plan, args.json)
        return 0

    if args.command == "cleanup":
        emit(cleanup_runtime(runtime, apply=bool(args.apply)), args.json)
        return 0

    if args.command == "test-notify":
        emit({"notification_started": notify_windows("Codex watchdog", args.message)}, args.json)
        return 0

    if args.command == "install":
        configured_name = runtime.config().get("task_name", DEFAULT_TASK_NAME)
        name = args.task_name or configured_name
        if name != configured_name and startup_entry_installed(configured_name):
            emit(
                {
                    "changed": False,
                    "reason": "existing_startup_entry_must_be_uninstalled_first",
                    "configured_name": configured_name,
                    "requested_name": name,
                },
                args.json,
            )
            return 0
        result = startup_entry(runtime, name, True, args.dry_run)
        if (result.get("changed") or result.get("reason") == "already_installed") and not args.dry_run:
            with FileLock(runtime.lock_path):
                config = runtime.config()
                config["task_name"] = name
                config["startup_command"] = result["command"]
                runtime.save_config(config)
        emit(result, args.json)
        return 0

    if args.command == "uninstall":
        current_config = runtime.config()
        configured_name = current_config.get("task_name", DEFAULT_TASK_NAME)
        name = args.task_name or configured_name
        result = startup_entry(
            runtime,
            name,
            False,
            args.dry_run,
            expected_command=current_config.get("startup_command"),
        )
        if result.get("changed") and not args.dry_run and name == configured_name:
            with FileLock(runtime.lock_path):
                config = runtime.config()
                config["task_name"] = DEFAULT_TASK_NAME
                config.pop("startup_command", None)
                runtime.save_config(config)
        emit(result, args.json)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
