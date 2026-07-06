"""
Unit tests for notify_usage_limit.py. Stdlib-only (unittest), no pytest
dependency required, matching the script's own zero-dependency policy.

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""
import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import notify_usage_limit as nul

REPO_ROOT = Path(__file__).resolve().parent.parent


class FindLimitMessageTests(unittest.TestCase):
    def test_matches_with_epoch(self):
        matched, epoch = nul.find_limit_message("Claude AI usage limit reached|1735700000")
        self.assertTrue(matched)
        self.assertEqual(epoch, 1735700000)

    def test_matches_case_insensitive_with_extra_spaces(self):
        matched, epoch = nul.find_limit_message("claude   usage limit reached|42")
        self.assertTrue(matched)
        self.assertEqual(epoch, 42)

    def test_matches_text_only_fallback_without_epoch(self):
        matched, epoch = nul.find_limit_message("your weekly limit reached for today")
        self.assertTrue(matched)
        self.assertIsNone(epoch)

    def test_no_match_on_unrelated_text(self):
        matched, epoch = nul.find_limit_message("waiting for your input")
        self.assertFalse(matched)
        self.assertIsNone(epoch)

    def test_text_only_fallback_rejected_when_disallowed(self):
        # Assistant prose describing the tool must NOT match when text-only
        # matching is disabled (as it is for untrusted transcript scans).
        prose = "I added handling for the 'usage limit reached' notification."
        self.assertEqual(nul.find_limit_message(prose, allow_text_only=False), (False, None))
        # ...but the same call still detects a real epoch-bearing message.
        matched, epoch = nul.find_limit_message(
            "Claude AI usage limit reached|1735700000", allow_text_only=False
        )
        self.assertTrue(matched)
        self.assertEqual(epoch, 1735700000)

    def test_empty_text_does_not_match(self):
        matched, epoch = nul.find_limit_message("")
        self.assertFalse(matched)
        self.assertIsNone(epoch)


class FormatResetTimeTests(unittest.TestCase):
    def test_uses_explicit_timezone(self):
        with mock.patch.dict(os.environ, {"CLAUDE_LIMIT_NOTIFIER_TZ": "UTC"}):
            result = nul.format_reset_time(0)
        self.assertIn("1970-01-01 00:00", result)

    def test_invalid_timezone_falls_back_without_raising(self):
        with mock.patch.dict(os.environ, {"CLAUDE_LIMIT_NOTIFIER_TZ": "Not/AZone"}):
            result = nul.format_reset_time(0)
        self.assertTrue(result)


class ReadTranscriptTailTests(unittest.TestCase):
    def test_reads_string_content(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "t.jsonl"
            path.write_text(json.dumps({"message": {"content": "usage limit reached|99"}}) + "\n")
            text = nul.read_transcript_tail(str(path))
        self.assertIn("usage limit reached|99", text)

    def test_reads_content_block_list(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "t.jsonl"
            entry = {"message": {"content": [{"type": "text", "text": "usage limit reached|7"}]}}
            path.write_text(json.dumps(entry) + "\n")
            text = nul.read_transcript_tail(str(path))
        self.assertIn("usage limit reached|7", text)

    def test_missing_file_returns_empty_string(self):
        self.assertEqual(nul.read_transcript_tail("/no/such/file.jsonl"), "")

    def test_ignores_malformed_json_lines(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "t.jsonl"
            path.write_text("not json\n" + json.dumps({"message": {"content": "hi"}}) + "\n")
            text = nul.read_transcript_tail(str(path))
        self.assertEqual(text, "hi")


class DedupStateTests(unittest.TestCase):
    """already_notified/mark_notified must track multiple independent keys
    at once (e.g. a 'reached' key and a 'reset' key for the same epoch),
    not just the single most-recent key."""

    def setUp(self):
        self.tmpdir = TemporaryDirectory()
        self._orig_state_file = nul.STATE_FILE
        nul.STATE_FILE = Path(self.tmpdir.name) / "state.json"

    def tearDown(self):
        nul.STATE_FILE = self._orig_state_file
        self.tmpdir.cleanup()

    def test_not_notified_when_no_state_file_exists(self):
        self.assertFalse(nul.already_notified("123"))

    def test_marks_and_detects_same_key(self):
        nul.mark_notified("123")
        self.assertTrue(nul.already_notified("123"))
        self.assertFalse(nul.already_notified("456"))

    def test_tracks_multiple_independent_keys_simultaneously(self):
        nul.mark_notified("1735700000")
        nul.mark_notified("reset:1735700000")
        self.assertTrue(nul.already_notified("1735700000"))
        self.assertTrue(nul.already_notified("reset:1735700000"))

    def test_corrupt_state_file_is_treated_as_no_state(self):
        nul.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        nul.STATE_FILE.write_text("not json")
        self.assertFalse(nul.already_notified("123"))

    def test_old_keys_are_trimmed_to_max_tracked(self):
        for i in range(nul.MAX_TRACKED_KEYS + 5):
            nul.mark_notified(f"key-{i}")
        state = nul.load_state()
        self.assertLessEqual(len(state["notified_keys"]), nul.MAX_TRACKED_KEYS)
        self.assertIn(f"key-{nul.MAX_TRACKED_KEYS + 4}", state["notified_keys"])
        self.assertNotIn("key-0", state["notified_keys"])


class PendingResetTests(unittest.TestCase):
    """The persisted pending-reset set is the reboot-resilient fallback for
    the detached watcher: a reset notification must still fire on a later hook
    call if the watcher process died before the reset time."""

    def setUp(self):
        self.tmpdir = TemporaryDirectory()
        self._orig_state_file = nul.STATE_FILE
        nul.STATE_FILE = Path(self.tmpdir.name) / "state.json"

    def tearDown(self):
        nul.STATE_FILE = self._orig_state_file
        self.tmpdir.cleanup()

    def test_future_reset_is_not_due(self):
        nul.add_pending_reset(2000, "/proj")
        self.assertEqual(nul.pop_due_resets(now=1000), [])
        # Still pending for a later check.
        self.assertIn("2000", nul.load_state().get("pending_resets", {}))

    def test_past_reset_is_due_once_then_removed(self):
        nul.add_pending_reset(1000, "/proj")
        self.assertEqual(nul.pop_due_resets(now=2000), [(1000, "/proj")])
        # Consumed: not returned again, and no longer pending.
        self.assertEqual(nul.pop_due_resets(now=2000), [])
        self.assertNotIn("1000", nul.load_state().get("pending_resets", {}))

    def test_already_notified_reset_is_dropped_not_returned(self):
        nul.add_pending_reset(1000, "/proj")
        nul.mark_notified("reset:1000")  # watcher already sent it
        self.assertEqual(nul.pop_due_resets(now=2000), [])
        self.assertNotIn("1000", nul.load_state().get("pending_resets", {}))

    def test_notify_due_resets_sends_and_dedups(self):
        nul.add_pending_reset(1000, "/proj")
        sent = []
        with mock.patch.object(nul, "send_slack_message", lambda url, text: sent.append(text)):
            nul.notify_due_resets("http://webhook", now=2000)
            nul.notify_due_resets("http://webhook", now=2000)  # second call: nothing due
        self.assertEqual(len(sent), 1)
        self.assertIn("한도 초기화", sent[0])
        self.assertTrue(nul.already_notified("reset:1000"))


class DebugLogTests(unittest.TestCase):
    """append_debug_log is what makes a 'no notification arrived' report
    diagnosable after the fact — it must record every hook invocation
    (matched or not) and never grow without bound."""

    def setUp(self):
        self.tmpdir = TemporaryDirectory()
        self._orig_log_file = nul.DEBUG_LOG_FILE
        nul.DEBUG_LOG_FILE = Path(self.tmpdir.name) / "debug.log"

    def tearDown(self):
        nul.DEBUG_LOG_FILE = self._orig_log_file
        self.tmpdir.cleanup()

    def test_appends_one_json_line_per_call(self):
        nul.append_debug_log("Notification", "waiting for your input", False, None)
        nul.append_debug_log("Notification", "Claude AI usage limit reached|42", True, 42)
        lines = nul.DEBUG_LOG_FILE.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        entry = json.loads(lines[-1])
        self.assertEqual(entry["event"], "Notification")
        self.assertTrue(entry["matched"])
        self.assertEqual(entry["epoch"], 42)
        self.assertIn("usage limit reached", entry["message_snippet"])

    def test_truncates_long_messages(self):
        nul.append_debug_log("Stop", "x" * 5000, False, None)
        entry = json.loads(nul.DEBUG_LOG_FILE.read_text(encoding="utf-8").splitlines()[-1])
        self.assertLessEqual(len(entry["message_snippet"]), 300)

    def test_rotates_to_max_debug_log_lines(self):
        for i in range(nul.MAX_DEBUG_LOG_LINES + 10):
            nul.append_debug_log("Notification", f"msg-{i}", False, None)
        lines = nul.DEBUG_LOG_FILE.read_text(encoding="utf-8").splitlines()
        self.assertLessEqual(len(lines), nul.MAX_DEBUG_LOG_LINES)
        last_entry = json.loads(lines[-1])
        self.assertIn(f"msg-{nul.MAX_DEBUG_LOG_LINES + 9}", last_entry["message_snippet"])


class BuildSlackTextTests(unittest.TestCase):
    def test_includes_reset_time_when_epoch_given(self):
        text = nul.build_slack_text("/tmp/proj", 1735700000)
        self.assertIn("재설정 예정", text)
        self.assertIn("/tmp/proj", text)

    def test_omits_reset_time_when_epoch_missing(self):
        text = nul.build_slack_text("/tmp/proj", None)
        self.assertNotIn("재설정 예정", text)

    def test_test_flag_marks_message_as_test(self):
        text = nul.build_slack_text("/tmp/proj", 1735700000, is_test=True)
        self.assertIn("테스트 메시지", text)

    def test_reset_available_text_mentions_cwd(self):
        text = nul.build_reset_available_text("/tmp/proj")
        self.assertIn("/tmp/proj", text)
        self.assertIn("다시 사용", text)


class SendSlackMessageTests(unittest.TestCase):
    def test_success_posts_once(self):
        with mock.patch("notify_usage_limit.urllib.request.urlopen") as mocked:
            mocked.return_value.__enter__.return_value.read.return_value = b"ok"
            nul.send_slack_message("http://example.invalid/webhook", "hello")
        mocked.assert_called_once()

    def test_retries_three_times_then_raises_on_persistent_failure(self):
        with mock.patch(
            "notify_usage_limit.urllib.request.urlopen", side_effect=URLError("boom")
        ) as mocked, mock.patch("notify_usage_limit.time.sleep"):
            with self.assertRaises(RuntimeError):
                nul.send_slack_message("http://example.invalid/webhook", "hello")
        self.assertEqual(mocked.call_count, 3)


class SpawnResetWatcherBoundsTests(unittest.TestCase):
    def test_skips_spawn_when_epoch_far_in_the_past(self):
        with mock.patch("notify_usage_limit.subprocess.Popen") as popen:
            nul.spawn_reset_watcher(int(nul.time.time()) - 7200, "/tmp/proj")
        popen.assert_not_called()

    def test_skips_spawn_when_epoch_absurdly_far_in_future(self):
        far_future = int(nul.time.time()) + nul.MAX_WATCHER_DELAY_SECONDS + 3600
        with mock.patch("notify_usage_limit.subprocess.Popen") as popen:
            nul.spawn_reset_watcher(far_future, "/tmp/proj")
        popen.assert_not_called()

    def test_spawns_within_bounds(self):
        soon = int(nul.time.time()) + 60
        with mock.patch("notify_usage_limit.subprocess.Popen") as popen:
            nul.spawn_reset_watcher(soon, "/tmp/proj")
        popen.assert_called_once()
        args = popen.call_args[0][0]
        self.assertIn("--wait-reset", args)
        self.assertIn(str(soon), args)


def _run_mock_slack_server():
    received = []
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            body = json.loads(self.rfile.read(length))
            with lock:
                received.append(body["text"])
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, received


class EndToEndSubprocessTests(unittest.TestCase):
    """Drives the real script as a subprocess (as Claude Code would invoke
    it via hooks) against a local mock Slack server, exercising the full
    detect -> dedup -> notify -> spawn-watcher pipeline."""

    def setUp(self):
        self.server, self.thread, self.received = _run_mock_slack_server()
        self.webhook_url = f"http://127.0.0.1:{self.server.server_port}/mock"
        self.tmpdir = TemporaryDirectory()
        self.state_file = Path(self.tmpdir.name) / "state.json"
        self.log_file = Path(self.tmpdir.name) / "debug.log"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmpdir.cleanup()

    def _run_hook(self, message, extra_env=None):
        env = os.environ.copy()
        env["SLACK_WEBHOOK_URL"] = self.webhook_url
        env["CLAUDE_LIMIT_NOTIFIER_STATE_FILE"] = str(self.state_file)
        env["CLAUDE_LIMIT_NOTIFIER_LOG_FILE"] = str(self.log_file)
        env.update(extra_env or {})
        payload = json.dumps({"hook_event_name": "Notification", "message": message, "cwd": "/tmp/proj"})
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "notify_usage_limit.py")],
            input=payload,
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )

    def _run_hook_with_transcript(self, transcript_lines):
        env = os.environ.copy()
        env["SLACK_WEBHOOK_URL"] = self.webhook_url
        env["CLAUDE_LIMIT_NOTIFIER_STATE_FILE"] = str(self.state_file)
        env["CLAUDE_LIMIT_NOTIFIER_LOG_FILE"] = str(self.log_file)
        transcript = Path(self.tmpdir.name) / "transcript.jsonl"
        transcript.write_text("".join(json.dumps(x) + "\n" for x in transcript_lines))
        # Stop-style invocation: empty message, detection relies on transcript.
        payload = json.dumps(
            {
                "hook_event_name": "Stop",
                "message": "",
                "cwd": "/tmp/proj",
                "transcript_path": str(transcript),
            }
        )
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "notify_usage_limit.py")],
            input=payload,
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )

    def test_no_match_sends_nothing_and_exits_zero(self):
        result = self._run_hook("waiting for your input")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.received, [])

    def test_transcript_prose_mentioning_limit_does_not_notify(self):
        # Regression: the assistant discussing "usage limit reached" in the
        # transcript must not fire a (false) notification — only an epoch-
        # bearing message should. This is the exact silent false-positive the
        # debug log surfaced.
        result = self._run_hook_with_transcript(
            [{"message": {"content": "I updated the 'usage limit reached' handler and tests."}}]
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.received, [])

    def test_transcript_with_real_epoch_message_still_notifies(self):
        epoch = int(nul.time.time()) + 3600
        result = self._run_hook_with_transcript(
            [{"message": {"content": f"Claude AI usage limit reached|{epoch}"}}]
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(self.received), 1)
        self.assertIn("사용량 한도 도달", self.received[0])

    def test_limit_reached_sends_slack_message(self):
        epoch = int(nul.time.time()) + 3600
        result = self._run_hook(f"Claude AI usage limit reached|{epoch}")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(self.received), 1)
        self.assertIn("사용량 한도 도달", self.received[0])

    def test_duplicate_event_is_deduped(self):
        epoch = int(nul.time.time()) + 3600
        self._run_hook(f"Claude AI usage limit reached|{epoch}")
        self._run_hook(f"Claude AI usage limit reached|{epoch}")
        self.assertEqual(len(self.received), 1)

    def test_reset_watcher_fires_after_short_delay(self):
        epoch = int(nul.time.time()) + 3
        result = self._run_hook(f"Claude AI usage limit reached|{epoch}")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(self.received), 1)

        deadline = nul.time.time() + 10
        while len(self.received) < 2 and nul.time.time() < deadline:
            nul.time.sleep(0.5)

        self.assertEqual(len(self.received), 2, "reset-available message never arrived")
        self.assertIn("한도 초기화", self.received[1])

    def _run_check_resets(self):
        env = os.environ.copy()
        env["SLACK_WEBHOOK_URL"] = self.webhook_url
        env["CLAUDE_LIMIT_NOTIFIER_STATE_FILE"] = str(self.state_file)
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "notify_usage_limit.py"), "--check-resets"],
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )

    def test_check_resets_delivers_due_reset_from_state(self):
        # Simulates the OS scheduler path: a reset epoch was persisted (limit
        # was hit earlier) and its time has now passed, but the watcher died
        # (e.g. reboot). --check-resets must still deliver the notice — exactly
        # once — and clear the pending record.
        self.state_file.write_text(
            json.dumps({"pending_resets": {str(int(nul.time.time()) - 5): "/tmp/proj"}})
        )
        result = self._run_check_resets()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(self.received), 1)
        self.assertIn("한도 초기화", self.received[0])
        # Second run has nothing due -> no duplicate.
        self._run_check_resets()
        self.assertEqual(len(self.received), 1)

    def test_check_resets_ignores_future_reset(self):
        self.state_file.write_text(
            json.dumps({"pending_resets": {str(int(nul.time.time()) + 3600): "/tmp/proj"}})
        )
        result = self._run_check_resets()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.received, [])


if __name__ == "__main__":
    unittest.main()
