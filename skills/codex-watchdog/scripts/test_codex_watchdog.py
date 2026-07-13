import importlib.util
import contextlib
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


SCRIPT = Path(__file__).with_name("codex_watchdog.py")
SPEC = importlib.util.spec_from_file_location("codex_watchdog", SCRIPT)
wd = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(wd)

THREAD = "11111111-1111-4111-8111-111111111111"
TURN = "22222222-2222-4222-8222-222222222222"
PROCESS = "33333333-3333-4333-8333-333333333333"


def scope() -> str:
    return (
        f"session_loop{{thread_id={THREAD}}}:submission_dispatch{{x=1}}:"
        f'turn{{otel.name="session_task.turn" thread.id={THREAD} turn.id={TURN} model=test}}:'
        f"session_task.run:run_turn:run_sampling_request{{turn_id={TURN} model=test}}:"
        f"try_run_sampling_request{{turn_id={TURN} model=test}}:"
    )


def start_body(call_id: str = "call_A") -> str:
    return scope() + (
        " Output item item=CustomToolCall { status: Some(\"in_progress\"), "
        f'call_id: "{call_id}", name: "exec" }}'
    )


def done_body(call_id: str = "call_A") -> str:
    return scope() + (
        "handle_tool_call_with_source: tool call completed event.name=\"codex.tool_call\" "
        f"turn_id={TURN} tool_name=exec call_id={call_id} execution_started=true"
    )


def request_body() -> str:
    return scope() + 'stream_request{x=1}: endpoint="/responses" auth_header_attached=true'


def stream_body() -> str:
    return scope() + "stream_request{x=1}: unhandled responses event: codex.response.metadata"


def complete_body() -> str:
    return scope() + (
        "post sampling token usage input=1 output=1 "
        f"turn_id={TURN} model_needs_follow_up=false has_pending_input=false "
        "needs_follow_up=false"
    )


class WatchdogTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name) / ".codex"
        self.home.mkdir(parents=True)
        self.database = self.home / "logs_2.sqlite"
        self.connection = sqlite3.connect(self.database)
        self.connection.execute(
            """
            CREATE TABLE logs (
                id INTEGER PRIMARY KEY,
                ts INTEGER,
                ts_nanos INTEGER,
                level TEXT,
                target TEXT,
                feedback_log_body TEXT,
                module_path TEXT,
                file TEXT,
                line INTEGER,
                thread_id TEXT,
                process_uuid TEXT,
                estimated_bytes INTEGER
            )
            """
        )
        self.connection.commit()
        self.runtime = wd.Runtime(self.home)
        with wd.FileLock(self.runtime.lock_path):
            config = self.runtime.config()
            config.update(
                {
                    "enabled": True,
                    "notify": False,
                    "db_path": str(self.database),
                    "post_tool_seconds": 10,
                    "response_seconds": 20,
                    "critical_seconds": 30,
                    "reminder_seconds": 60,
                }
            )
            self.runtime.save_config(config)
            state = self.runtime.state()
            state["initialized"] = True
            state["last_log_id"] = 0
            self.runtime.save_state(state)

    def tearDown(self):
        self.connection.close()
        self.temporary.cleanup()

    def insert(self, row_id: int, stamp: int, target: str, body: str):
        self.connection.execute(
            "INSERT INTO logs(id,ts,ts_nanos,level,target,feedback_log_body,"
            "thread_id,process_uuid,estimated_bytes) VALUES(?,?,?,?,?,?,?,?,?)",
            (row_id, stamp, 0, "TRACE", target, body, THREAD, PROCESS, len(body)),
        )
        self.connection.commit()

    def incidents(self):
        return wd.read_incidents(self.runtime, 100, True)

    def test_normal_post_tool_continuation_does_not_alert(self):
        now = int(time.time()) - 100
        self.insert(1, now, wd.TARGET_TOOL_START, start_body())
        self.insert(2, now + 1, wd.TARGET_TOOL_DONE, done_body())
        self.insert(3, now + 2, wd.TARGET_REQUEST, request_body())
        self.insert(4, now + 3, wd.TARGET_STREAM, stream_body())
        result = wd.run_once(self.runtime)
        self.assertEqual(result["incidents"], 0)
        self.assertEqual(self.incidents(), [])

    def test_post_tool_without_request_alerts(self):
        now = int(time.time()) - 40
        self.insert(1, now, wd.TARGET_TOOL_START, start_body())
        self.insert(2, now + 1, wd.TARGET_TOOL_DONE, done_body())
        result = wd.run_once(self.runtime)
        self.assertEqual(result["incidents"], 1)
        self.assertEqual(self.incidents()[0]["kind"], "post_tool_no_request")
        self.assertEqual(self.incidents()[0]["severity"], "critical")

    def test_request_without_first_event_alerts(self):
        now = int(time.time()) - 35
        self.insert(1, now, wd.TARGET_REQUEST, request_body())
        result = wd.run_once(self.runtime)
        self.assertEqual(result["incidents"], 1)
        self.assertEqual(self.incidents()[0]["kind"], "request_no_first_event")

    def test_request_clears_orphaned_active_call_and_is_not_masked(self):
        now = int(time.time())
        self.insert(1, now - 700, wd.TARGET_TOOL_START, start_body("call_orphan"))
        self.insert(2, now - 35, wd.TARGET_REQUEST, request_body())
        result = wd.run_once(self.runtime)
        records = self.incidents()
        self.assertEqual(result["incidents"], 1)
        self.assertEqual([record["kind"] for record in records], ["request_no_first_event"])
        turn = next(iter(self.runtime.state()["turns"].values()))
        self.assertEqual(turn["active_calls"], [])
        self.assertEqual(turn["active_call_started_at"], {})

    def test_late_tool_completion_cannot_roll_request_back_to_post_tool(self):
        now = int(time.time())
        self.insert(1, now - 700, wd.TARGET_TOOL_START, start_body("call_late"))
        self.insert(2, now - 35, wd.TARGET_REQUEST, request_body())
        self.insert(3, now - 34, wd.TARGET_TOOL_DONE, done_body("call_late"))
        result = wd.run_once(self.runtime)
        records = self.incidents()
        self.assertEqual(result["incidents"], 1)
        self.assertEqual(records[0]["kind"], "request_no_first_event")
        turn = next(iter(self.runtime.state()["turns"].values()))
        self.assertEqual(turn["phase"], "request_pending")
        self.assertIsNone(turn["post_tool_at"])

    def test_stream_clears_orphaned_active_call(self):
        now = int(time.time())
        self.insert(1, now - 700, wd.TARGET_TOOL_START, start_body("call_orphan"))
        self.insert(2, now - 1, wd.TARGET_STREAM, stream_body())
        result = wd.run_once(self.runtime)
        self.assertEqual(result["incidents"], 0)
        turn = next(iter(self.runtime.state()["turns"].values()))
        self.assertEqual(turn["phase"], "streaming")
        self.assertEqual(turn["active_calls"], [])

    def test_terminal_event_clears_orphaned_active_call(self):
        now = int(time.time())
        self.insert(1, now - 700, wd.TARGET_TOOL_START, start_body("call_orphan"))
        self.insert(2, now - 1, wd.TARGET_COMPLETE, complete_body())
        result = wd.run_once(self.runtime)
        self.assertEqual(result["incidents"], 0)
        turn = next(iter(self.runtime.state()["turns"].values()))
        self.assertEqual(turn["phase"], "terminal")
        self.assertEqual(turn["active_calls"], [])

    def test_parallel_calls_wait_until_all_are_done(self):
        now = int(time.time()) - 40
        self.insert(1, now, wd.TARGET_TOOL_START, start_body("call_A"))
        self.insert(2, now, wd.TARGET_TOOL_START, start_body("call_B"))
        self.insert(3, now + 1, wd.TARGET_TOOL_DONE, done_body("call_B"))
        wd.run_once(self.runtime)
        self.assertEqual(self.incidents(), [])
        state = self.runtime.state()
        turn = next(iter(state["turns"].values()))
        self.assertEqual(turn["active_calls"], ["call_A"])
        self.insert(4, now + 2, wd.TARGET_TOOL_DONE, done_body("call_A"))
        wd.run_once(self.runtime)
        self.assertEqual(self.incidents()[0]["kind"], "post_tool_no_request")

    def test_tool_timeout_is_per_call_and_critical_writes_recovery_manifest(self):
        with wd.FileLock(self.runtime.lock_path):
            config = self.runtime.config()
            config["tool_warning_seconds"] = 10
            config["tool_critical_seconds"] = 30
            config["reminder_seconds"] = 1
            self.runtime.save_config(config)
        rollout = (
            self.home
            / "sessions"
            / "2026"
            / "07"
            / "13"
            / f"rollout-example-{THREAD}.jsonl"
        )
        rollout.parent.mkdir(parents=True)
        rollout.write_bytes(b"x" * 321)
        now = int(time.time())
        self.insert(1, now - 40, wd.TARGET_TOOL_START, start_body("call_stale"))
        self.insert(2, now - 5, wd.TARGET_TOOL_START, start_body("call_recent"))
        result = wd.run_once(self.runtime)
        self.assertEqual(result["incidents"], 1)
        incident = self.incidents()[0]
        self.assertEqual(incident["kind"], "tool_running_no_completion")
        self.assertEqual(incident["severity"], "critical")
        self.assertEqual(incident["call_id"], "call_stale")
        manifest_path = Path(incident["recovery_manifest"])
        self.assertTrue(manifest_path.is_file())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(
            set(manifest),
            {
                "thread_id",
                "turn_id",
                "phase",
                "active_calls",
                "event_times",
                "rollout",
                "recommendations",
            },
        )
        self.assertEqual(manifest["rollout"]["path"], str(rollout.resolve()))
        self.assertEqual(manifest["rollout"]["size_bytes"], 321)
        self.assertEqual(
            [call["call_id"] for call in manifest["active_calls"]],
            ["call_recent", "call_stale"],
        )
        manifest_files_before = list(self.runtime.recovery_dir.glob("*.json"))
        state = self.runtime.state()
        repeated = wd.check_turn_incidents(
            self.runtime,
            state,
            self.runtime.config(),
            time.time() + 2,
        )
        self.assertEqual(len(repeated), 1)
        self.assertEqual(repeated[0]["recovery_manifest"], str(manifest_path))
        self.assertEqual(list(self.runtime.recovery_dir.glob("*.json")), manifest_files_before)

    def test_tool_warning_uses_configured_threshold(self):
        with wd.FileLock(self.runtime.lock_path):
            config = self.runtime.config()
            config["tool_warning_seconds"] = 10
            config["tool_critical_seconds"] = 60
            self.runtime.save_config(config)
        now = int(time.time())
        self.insert(1, now - 20, wd.TARGET_TOOL_START, start_body("call_warning"))
        wd.run_once(self.runtime)
        incident = self.incidents()[0]
        self.assertEqual(incident["severity"], "warning")
        self.assertNotIn("recovery_manifest", incident)

    def test_parallel_critical_calls_share_one_recovery_manifest(self):
        with wd.FileLock(self.runtime.lock_path):
            config = self.runtime.config()
            config["tool_warning_seconds"] = 10
            config["tool_critical_seconds"] = 30
            self.runtime.save_config(config)
        now = int(time.time())
        self.insert(1, now - 40, wd.TARGET_TOOL_START, start_body("call_A"))
        self.insert(2, now - 40, wd.TARGET_TOOL_START, start_body("call_B"))
        wd.run_once(self.runtime)
        incidents = self.incidents()
        self.assertEqual(len(incidents), 2)
        manifests = {incident["recovery_manifest"] for incident in incidents}
        self.assertEqual(len(manifests), 1)
        self.assertEqual(len(list(self.runtime.recovery_dir.glob("*.json"))), 1)

    def test_parallel_incidents_share_one_windows_notification(self):
        incidents = [
            wd.make_incident(
                "tool_running_no_completion",
                "warning",
                181 + index,
                thread_id=THREAD,
                turn_id=TURN,
                call_id=f"call_{index}",
            )
            for index in range(5)
        ]
        with mock.patch.object(wd, "notify_windows", return_value=True) as notify:
            wd.notify_incidents({"notify": True}, incidents)
        notify.assert_called_once()
        self.assertIn("5 calls", notify.call_args.args[1])

    def test_active_automatic_call_is_never_pruned_by_age_or_count(self):
        now = time.time()
        active = wd.fresh_turn(PROCESS, THREAD, TURN, now - 200000)
        active["active_calls"] = ["call_long"]
        active["active_call_started_at"] = {"call_long": now - 200000}
        turns = {"active": active}
        for index in range(300):
            turns[f"inactive-{index}"] = {
                "last_event_at": now - 200000 - index,
                "terminal": "completed",
                "active_calls": [],
            }
        state = {"turns": turns}
        wd.prune_turns(state, now)
        self.assertIn("active", state["turns"])
        self.assertEqual(state["turns"]["active"]["active_calls"], ["call_long"])

    def test_arm_tags_are_unique_and_exact(self):
        class Args:
            thread = THREAD
            turn = TURN
            kind = "image"
            generation = 1
            timeout_seconds = 180
            label = "concept"

        first = wd.arm_job(self.runtime, Args())
        second = wd.arm_job(self.runtime, Args())
        self.assertTrue(first["armed"])
        self.assertNotEqual(first["tag"], second["tag"])
        updated = wd.update_job(self.runtime, first["tag"], "heartbeat", "file timestamp moved")
        self.assertTrue(updated["updated"])
        closed = wd.update_job(self.runtime, first["tag"], "disarm", "completed")
        self.assertEqual(closed["state"], "disarmed")
        jobs = self.runtime.jobs()["jobs"]
        self.assertEqual([job["state"] for job in jobs], ["disarmed", "armed"])

    def test_arm_rejects_manifest_invalid_timeout_and_generation(self):
        invalid_arguments = (
            ["arm", "--kind", "shell", "--timeout-seconds", "0"],
            ["arm", "--kind", "shell", "--generation", "-1"],
        )
        for command in invalid_arguments:
            with self.subTest(command=command):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        wd.main(["--home", str(self.home), *command])
        self.assertEqual(self.runtime.jobs()["jobs"], [])

    def test_infer_turn_streams_rows_and_stops_without_fetchall(self):
        class Cursor:
            def fetchall(self):
                raise AssertionError("infer_turn must not fetchall log bodies")

            def __iter__(self):
                yield wd.TARGET_REQUEST, request_body()
                raise AssertionError("infer_turn did not stop at the first matching turn")

        class Connection:
            def execute(self, *_args, **_kwargs):
                return Cursor()

            def close(self):
                pass

        original_open_logs = wd.open_logs
        try:
            wd.open_logs = lambda _path: Connection()
            self.assertEqual(wd.infer_turn(self.runtime, THREAD), TURN)
        finally:
            wd.open_logs = original_open_logs

    def test_stale_armed_job_records_one_deduplicated_incident(self):
        class Args:
            thread = THREAD
            turn = TURN
            kind = "shell"
            generation = 1
            timeout_seconds = 1
            label = "slow command"

        armed = wd.arm_job(self.runtime, Args())
        with wd.FileLock(self.runtime.lock_path):
            jobs_doc = self.runtime.jobs()
            jobs_doc["jobs"][0]["last_progress_at"] = time.time() - 10
            self.runtime.save_jobs(jobs_doc)
        first = wd.run_once(self.runtime)
        second = wd.run_once(self.runtime)
        self.assertEqual(first["incidents"], 1)
        self.assertEqual(second["incidents"], 0)
        records = self.incidents()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["kind"], "armed_job_no_verified_progress")
        self.assertEqual(records[0]["tag"], armed["tag"])

    def test_initialization_starts_at_log_tail(self):
        now = int(time.time()) - 100
        self.insert(1, now, wd.TARGET_REQUEST, request_body())
        with wd.FileLock(self.runtime.lock_path):
            state = self.runtime.state()
            state["initialized"] = False
            state["last_log_id"] = 0
            self.runtime.save_state(state)
        result = wd.run_once(self.runtime)
        self.assertEqual(result["database"], "initialized_from_tail")
        self.assertEqual(self.incidents(), [])
        self.assertEqual(self.runtime.state()["last_log_id"], 1)

    def test_log_polling_uses_bounded_batches_and_advances_cursor(self):
        with wd.FileLock(self.runtime.lock_path):
            config = self.runtime.config()
            config["log_batch_rows"] = 2
            self.runtime.save_config(config)
        now = int(time.time())
        for row_id in range(1, 6):
            self.insert(row_id, now + row_id, wd.TARGET_REQUEST, request_body())
        self.insert(6, now + 6, "unmatched.target", "ignored")
        first = wd.run_once(self.runtime)
        self.assertEqual(first["rows"], 2)
        self.assertEqual(self.runtime.state()["last_log_id"], 2)
        second = wd.run_once(self.runtime)
        self.assertEqual(second["rows"], 2)
        self.assertEqual(self.runtime.state()["last_log_id"], 4)
        third = wd.run_once(self.runtime)
        self.assertEqual(third["rows"], 1)
        self.assertEqual(self.runtime.state()["last_log_id"], 6)

    def test_idle_poll_does_not_rewrite_state_or_jobs(self):
        wd.run_once(self.runtime)  # Establish the database identity once.
        state_mtime = self.runtime.state_path.stat().st_mtime_ns
        jobs_mtime = self.runtime.jobs_path.stat().st_mtime_ns
        time.sleep(0.02)
        wd.run_once(self.runtime)
        self.assertEqual(self.runtime.state_path.stat().st_mtime_ns, state_mtime)
        self.assertEqual(self.runtime.jobs_path.stat().st_mtime_ns, jobs_mtime)

    def test_truncated_log_database_resets_from_tail_and_clears_stale_turns(self):
        now = int(time.time())
        self.insert(1, now, wd.TARGET_REQUEST, request_body())
        with wd.FileLock(self.runtime.lock_path):
            state = self.runtime.state()
            state["last_log_id"] = 999
            state["turns"] = {"stale": {"phase": "request_pending"}}
            self.runtime.save_state(state)
        result = wd.run_once(self.runtime)
        self.assertEqual(result["database"], "reset_from_tail")
        state = self.runtime.state()
        self.assertEqual(state["last_log_id"], 1)
        self.assertEqual(state["turns"], {})
        self.assertEqual(self.incidents(), [])

    def test_cleanup_dry_run_and_apply_bound_closed_jobs_only(self):
        now = time.time()
        jobs = []
        for index in range(510):
            jobs.append(
                {
                    "tag": f"closed-{index}",
                    "state": "disarmed",
                    "disarmed_at": wd.iso_time(now - index),
                }
            )
        for index in range(2):
            jobs.append(
                {
                    "tag": f"old-{index}",
                    "state": "disarmed",
                    "disarmed_at": wd.iso_time(now - wd.DISARMED_RETENTION_SECONDS - 10 - index),
                }
            )
        jobs.extend(
            [
                {"tag": "active-old", "state": "armed", "disarmed_at": None},
                {"tag": "stalled-old", "state": "stalled", "disarmed_at": None},
            ]
        )
        with wd.FileLock(self.runtime.lock_path):
            document = self.runtime.jobs()
            document["jobs"] = jobs
            self.runtime.save_jobs(document)
        dry_run = wd.cleanup_runtime(self.runtime, apply=False)
        self.assertEqual(dry_run["mode"], "dry-run")
        self.assertEqual(dry_run["jobs"]["removed"], 12)
        self.assertEqual(len(self.runtime.jobs()["jobs"]), 514)
        applied = wd.cleanup_runtime(self.runtime, apply=True)
        self.assertEqual(applied["mode"], "apply")
        retained = self.runtime.jobs()["jobs"]
        self.assertEqual(len(retained), 502)
        tags = {job["tag"] for job in retained}
        self.assertIn("active-old", tags)
        self.assertIn("stalled-old", tags)
        self.assertEqual(applied["scope"], str((self.home / "watchdog").resolve()))
        self.assertFalse(applied["codex_data_touched"])

    def test_regular_poll_automatically_prunes_expired_closed_jobs(self):
        now = time.time()
        with wd.FileLock(self.runtime.lock_path):
            document = self.runtime.jobs()
            document["jobs"] = [
                {
                    "tag": "expired",
                    "state": "disarmed",
                    "disarmed_at": wd.iso_time(now - wd.DISARMED_RETENTION_SECONDS - 1),
                },
                {"tag": "active", "state": "armed", "last_progress_at": now},
            ]
            self.runtime.save_jobs(document)
        wd.run_once(self.runtime)
        jobs = self.runtime.jobs()["jobs"]
        self.assertEqual([job["tag"] for job in jobs], ["active"])

    def test_incident_log_rotates_atomically_with_three_backups(self):
        original_limit = wd.INCIDENT_MAX_BYTES
        try:
            wd.INCIDENT_MAX_BYTES = 260
            for sequence in range(10):
                self.runtime.append_incident(
                    {"sequence": sequence, "payload": "x" * 150}
                )
            backups = self.runtime.incident_files()
            self.assertTrue(all(path.exists() for path in backups))
            self.assertFalse(
                self.runtime.incidents_path.with_name(
                    self.runtime.incidents_path.name + ".4"
                ).exists()
            )
            records = wd.read_incidents(self.runtime, 100, True)
            self.assertEqual(records[-1]["sequence"], 9)
            self.assertLessEqual(len(backups), wd.INCIDENT_BACKUP_COUNT)
        finally:
            wd.INCIDENT_MAX_BYTES = original_limit

    def test_all_compatibility_output_is_hard_capped(self):
        records = [{"sequence": index} for index in range(600)]
        self.runtime.incidents_path.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )
        incidents = wd.read_incidents(self.runtime, 10_000, True)
        self.assertEqual(len(incidents), wd.MAX_OUTPUT_RECORDS)
        self.assertEqual(incidents[0]["sequence"], 100)
        jobs, truncated = wd.bounded_jobs(
            [{"state": "disarmed", "tag": str(index)} for index in range(600)],
            True,
            10_000,
        )
        self.assertTrue(truncated)
        self.assertEqual(len(jobs), wd.MAX_OUTPUT_RECORDS)

    def test_daemon_survives_transient_sqlite_error_until_disabled(self):
        bad_database = self.home / "bad.sqlite"
        bad_database.write_text("not a sqlite database", encoding="utf-8")
        with wd.FileLock(self.runtime.lock_path):
            config = self.runtime.config()
            config["db_path"] = str(bad_database)
            config["poll_seconds"] = 1
            config["enabled"] = True
            self.runtime.save_config(config)
        process = subprocess.Popen(
            [sys.executable, str(SCRIPT), "--home", str(self.home), "run"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.time() + 4
            while time.time() < deadline and not self.runtime.error_path.exists():
                time.sleep(0.1)
            self.assertTrue(self.runtime.error_path.exists())
            self.assertIsNone(process.poll(), "daemon exited after a SQLite error")
            subprocess.run(
                [sys.executable, str(SCRIPT), "--home", str(self.home), "disable"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=6,
            )
            process.wait(timeout=6)
            self.assertEqual(process.returncode, 0)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
