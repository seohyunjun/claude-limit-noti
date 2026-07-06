#!/usr/bin/env python3
"""
Claude Code usage-limit -> Slack webhook notifier.

Register this script as a Claude Code hook (Notification and/or Stop).
When your Claude subscription (Pro/Max) usage limit is hit, Claude Code
surfaces a message containing a pattern such as:

    Claude AI usage limit reached|<unix_epoch_seconds>

This script watches hook input (and, as a fallback, the tail of the
session transcript) for that pattern and posts a message to a Slack
Incoming Webhook when it fires. A small state file prevents the same
limit window from re-notifying on every subsequent hook call.

When a new limit-reached event is detected, this script also spawns a
detached background process (via `--wait-reset`, an internal flag -
not meant to be invoked directly) that sleeps until the reset epoch
and then posts a second "usage available again" Slack message. This
works even if Claude Code itself isn't running at that moment, since
the watcher is a separate OS process; it does not survive a reboot or
the machine going to sleep.

To make that reset notification robust across reboots/sleep, the reset
epoch is also persisted to the state file, and a lightweight
`--check-resets` mode posts any reset whose time has passed. Register
`--check-resets` with an OS-level scheduler (cron / systemd timer /
launchd) so the notice fires even if Claude Code never runs again:

    python3 /path/to/notify_usage_limit.py --check-resets

Usage as a Claude Code hook (settings.json):
    {
      "hooks": {
        "Notification": [{"hooks": [{"type": "command", "command": "python3 /path/to/notify_usage_limit.py"}]}],
        "Stop":         [{"hooks": [{"type": "command", "command": "python3 /path/to/notify_usage_limit.py"}]}]
      }
    }

Required environment variable:
    SLACK_WEBHOOK_URL   Slack Incoming Webhook URL

Optional environment variables:
    CLAUDE_LIMIT_NOTIFIER_TZ          IANA timezone for the displayed reset time (default: system local tz)
    CLAUDE_LIMIT_NOTIFIER_STATE_FILE  Path to the dedup state file (default: ~/.claude/usage-limit-notifier-state.json)
    CLAUDE_LIMIT_NOTIFIER_DEBUG       Set to any value to print debug logs to stderr

Manual test:
    SLACK_WEBHOOK_URL=https://hooks.slack.com/services/... python3 notify_usage_limit.py --test
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

STATE_FILE = Path(
    os.environ.get(
        "CLAUDE_LIMIT_NOTIFIER_STATE_FILE",
        str(Path.home() / ".claude" / "usage-limit-notifier-state.json"),
    )
)

# Every hook invocation appends one line here (event name + matched? + a
# truncated copy of the message), capped to the last MAX_DEBUG_LOG_LINES
# entries. This is what makes a silent "no notification arrived" reported
# by a user diagnosable after the fact: it shows whether the hook fired at
# all, and if it did, whether the real message matched LIMIT_PATTERNS.
DEBUG_LOG_FILE = Path(
    os.environ.get(
        "CLAUDE_LIMIT_NOTIFIER_LOG_FILE",
        str(Path.home() / ".claude" / "usage-limit-notifier-debug.log"),
    )
)
MAX_DEBUG_LOG_LINES = 200

# How many dedup keys to remember (one "reached" key + one "reset" key per
# limit window, plus some headroom); prevents the state file from growing
# forever across many limit windows.
MAX_TRACKED_KEYS = 20

# A weekly usage limit is the longest window Claude Code currently has;
# refuse to schedule a watcher further out than this as a sanity guard
# against a mis-parsed epoch spawning a process that sleeps for years.
MAX_WATCHER_DELAY_SECONDS = 8 * 24 * 3600

# Patterns that carry a machine-readable reset epoch (the "|<digits>" suffix).
# These are strict enough to trust anywhere, including untrusted transcript
# prose, because ordinary conversation does not emit that exact suffix.
EPOCH_PATTERNS = [
    re.compile(r"claude\s*(ai)?\s*usage limit reached\|(\d+)", re.IGNORECASE),
    re.compile(r"usage limit reached\|(\d+)", re.IGNORECASE),
]

# A text-only fallback in case Claude Code changes the machine-readable format.
# It is intentionally loose, so it is ONLY trusted for the hook's own `message`
# field (a controlled notification string) and NOT for transcript prose: the
# assistant discussing usage limits (including this very tool) would otherwise
# match and fire a false notification. See find_limit_message(allow_text_only).
TEXT_ONLY_PATTERN = re.compile(
    r"(usage limit reached|5-hour limit reached|weekly limit reached|limit will reset)",
    re.IGNORECASE,
)

# Kept as the full ordered list (epoch patterns first, then the text-only
# fallback) for callers/tests that want the permissive behavior.
LIMIT_PATTERNS = EPOCH_PATTERNS + [TEXT_ONLY_PATTERN]


def log(*args):
    if os.environ.get("CLAUDE_LIMIT_NOTIFIER_DEBUG"):
        print("[usage-limit-notifier]", *args, file=sys.stderr)


def append_debug_log(event_name: str, message: str, matched: bool, epoch) -> None:
    """Record every hook invocation so a "no notification arrived" report is
    diagnosable afterwards: did the hook even fire, and if so, did the real
    message match LIMIT_PATTERNS? Truncates the message to avoid bloating the
    log with unrelated conversation content, and keeps only the most recent
    MAX_DEBUG_LOG_LINES entries."""
    entry = {
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "event": event_name,
        "matched": matched,
        "epoch": epoch,
        "message_snippet": (message or "")[:300],
    }
    try:
        DEBUG_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        if DEBUG_LOG_FILE.exists():
            lines = DEBUG_LOG_FILE.read_text(encoding="utf-8").splitlines()
        lines.append(json.dumps(entry, ensure_ascii=False))
        lines = lines[-MAX_DEBUG_LOG_LINES:]
        DEBUG_LOG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        log("could not write debug log:", exc)


def find_limit_message(text: str, allow_text_only: bool = True):
    """Return (matched, reset_epoch_or_None) for the first pattern that hits.

    allow_text_only gates the loose, epoch-less fallback. Leave it True for the
    hook's trusted `message` field, but pass False when scanning transcript
    prose so ordinary conversation mentioning "usage limit reached" (including
    this tool describing itself) cannot fire a false notification; transcript
    scans then only trust patterns carrying a machine-readable reset epoch."""
    if not text:
        return False, None
    patterns = EPOCH_PATTERNS + [TEXT_ONLY_PATTERN] if allow_text_only else EPOCH_PATTERNS
    for pattern in patterns:
        m = pattern.search(text)
        if not m:
            continue
        epoch = None
        for group in reversed(m.groups() or ()):
            if group and group.isdigit():
                epoch = int(group)
                break
        return True, epoch
    return False, None


def read_transcript_tail(transcript_path: str, max_lines: int = 30) -> str:
    """Best-effort scan of the tail of a transcript JSONL file for message text."""
    try:
        path = Path(transcript_path)
        if not path.is_file():
            return ""
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError as exc:
        log("could not read transcript:", exc)
        return ""

    chunks = []
    for line in lines[-max_lines:]:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        content = (entry.get("message") or {}).get("content")
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    chunks.append(block["text"])
    return "\n".join(chunks)


def format_reset_time(epoch: int) -> str:
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    tz_name = os.environ.get("CLAUDE_LIMIT_NOTIFIER_TZ")
    if tz_name:
        try:
            from zoneinfo import ZoneInfo

            dt = dt.astimezone(ZoneInfo(tz_name))
        except Exception as exc:
            log("invalid CLAUDE_LIMIT_NOTIFIER_TZ, falling back to local tz:", exc)
            dt = dt.astimezone()
    else:
        dt = dt.astimezone()
    return dt.strftime("%Y-%m-%d %H:%M %Z")


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    except OSError as exc:
        log("could not write state file:", exc)


def already_notified(dedup_key: str) -> bool:
    return dedup_key in load_state().get("notified_keys", [])


def add_pending_reset(epoch: int, cwd: str) -> None:
    """Persist a reset epoch so the "usage available again" notification can
    still fire from a later hook invocation even if the detached watcher
    process dies (e.g. the machine reboots or sleeps before the reset time)."""
    state = load_state()
    pending = state.get("pending_resets", {})
    pending[str(epoch)] = cwd or ""
    # Cap size; keep the most recent reset windows.
    if len(pending) > MAX_TRACKED_KEYS:
        for stale in sorted(pending, key=int)[:-MAX_TRACKED_KEYS]:
            del pending[stale]
    state["pending_resets"] = pending
    save_state(state)


def pop_due_resets(now: float) -> list:
    """Return [(epoch, cwd), ...] for pending resets whose time has passed and
    that were not already announced, removing them from the pending set. This
    is the reboot-resilient counterpart to the detached watcher: the reset
    notification fires on the next hook call after the reset time, at latest."""
    state = load_state()
    pending = state.get("pending_resets", {})
    if not pending:
        return []
    notified = set(state.get("notified_keys", []))
    due = []
    remaining = {}
    for epoch_str, cwd in pending.items():
        try:
            epoch = int(epoch_str)
        except (TypeError, ValueError):
            continue
        if epoch <= now and f"reset:{epoch}" not in notified:
            due.append((epoch, cwd))
        elif epoch > now:
            remaining[epoch_str] = cwd
        # (already-notified & due entries are simply dropped)
    state["pending_resets"] = remaining
    save_state(state)
    return due


def notify_due_resets(webhook_url: str, now: float) -> None:
    """Send any reset notifications that are due (fallback for a dead watcher)."""
    for epoch, cwd in pop_due_resets(now):
        dedup_key = f"reset:{epoch}"
        if already_notified(dedup_key):
            continue
        if not webhook_url:
            log("reset is due but SLACK_WEBHOOK_URL is not set")
            return
        try:
            send_slack_message(webhook_url, build_reset_available_text(cwd))
            mark_notified(dedup_key)
        except RuntimeError as exc:
            log(str(exc))


def mark_notified(dedup_key: str) -> None:
    state = load_state()
    keys = state.get("notified_keys", [])
    if dedup_key not in keys:
        keys.append(dedup_key)
    state["notified_keys"] = keys[-MAX_TRACKED_KEYS:]
    state["last_notified_at"] = time.time()
    save_state(state)


def send_slack_message(webhook_url: str, text: str) -> None:
    payload = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    last_error = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
            return
        except (urllib.error.URLError, urllib.error.HTTPError) as exc:
            last_error = exc
            log(f"slack post failed (attempt {attempt + 1}/3):", exc)
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"failed to notify Slack: {last_error}")


def build_slack_text(cwd: str, reset_epoch, is_test: bool = False) -> str:
    title = "Claude Code 사용량 한도 도달"
    if is_test:
        title += " (테스트 메시지 - 재설정 시각은 실제 값이 아닌 임의값입니다)"
    lines = [f":warning: *{title}*"]
    if cwd:
        lines.append(f"- 프로젝트 경로: `{cwd}`")
    if reset_epoch:
        lines.append(f"- 재설정 예정: `{format_reset_time(reset_epoch)}`")
    now_str = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    lines.append(f"- 감지 시각: `{now_str}`")
    return "\n".join(lines)


def build_reset_available_text(cwd: str) -> str:
    lines = [":white_check_mark: *Claude Code 사용량 한도 초기화*"]
    if cwd:
        lines.append(f"- 프로젝트 경로: `{cwd}`")
    lines.append("- 이제 다시 사용할 수 있어요.")
    return "\n".join(lines)


def spawn_reset_watcher(epoch: int, cwd: str) -> None:
    """Spawn a detached process that waits for `epoch` and posts a follow-up
    Slack message once the usage limit resets. Runs as a separate OS process
    so it keeps waiting even after this hook invocation (and Claude Code)
    exits; it will not survive the machine rebooting or going to sleep."""
    delay = epoch - time.time()
    if delay < -3600 or delay > MAX_WATCHER_DELAY_SECONDS:
        log(f"not spawning reset watcher, delay out of bounds: {delay:.0f}s")
        return

    cmd = [sys.executable, str(Path(__file__).resolve()), "--wait-reset", str(epoch), "--cwd", cwd]
    popen_kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    else:
        popen_kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    try:
        subprocess.Popen(cmd, **popen_kwargs)
        log(f"spawned reset watcher for epoch {epoch} (delay={delay:.0f}s)")
    except OSError as exc:
        log("failed to spawn reset watcher:", exc)


def wait_and_notify_reset(epoch: int, cwd: str) -> int:
    dedup_key = f"reset:{epoch}"
    if already_notified(dedup_key):
        return 0

    delay = epoch - time.time()
    if delay > 0:
        time.sleep(delay)

    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook_url:
        log("reset watcher fired but SLACK_WEBHOOK_URL is not set")
        return 0

    try:
        send_slack_message(webhook_url, build_reset_available_text(cwd))
        mark_notified(dedup_key)
    except RuntimeError as exc:
        log(str(exc))
    return 0


def main() -> int:
    if "--wait-reset" in sys.argv:
        idx = sys.argv.index("--wait-reset")
        epoch = int(sys.argv[idx + 1])
        cwd = ""
        if "--cwd" in sys.argv:
            cwd_idx = sys.argv.index("--cwd")
            cwd = sys.argv[cwd_idx + 1]
        return wait_and_notify_reset(epoch, cwd)

    if "--check-resets" in sys.argv:
        # Meant to be run by an OS-level scheduler (cron / systemd timer /
        # launchd), NOT as a Claude Code hook. It posts any reset notification
        # whose time has passed and that a dead watcher never delivered, so the
        # "usage available again" notice arrives even if Claude Code never runs
        # again after a reboot/sleep. Reads only persisted state; no stdin.
        notify_due_resets(os.environ.get("SLACK_WEBHOOK_URL"), time.time())
        return 0

    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")

    if "--test" in sys.argv:
        if not webhook_url:
            print("SLACK_WEBHOOK_URL is not set.", file=sys.stderr)
            return 1
        send_slack_message(
            webhook_url,
            build_slack_text("(test)", int(time.time()) + 3600, is_test=True),
        )
        print("Test message sent. (재설정 시각은 실제 값이 아니라 '지금+1시간' 더미값입니다)")
        return 0

    try:
        hook_input = json.load(sys.stdin)
    except json.JSONDecodeError:
        hook_input = {}

    event_name = hook_input.get("hook_event_name", "")
    message = hook_input.get("message", "") or ""
    transcript_path = hook_input.get("transcript_path", "")
    cwd = hook_input.get("cwd", "")

    log(f"event={event_name} message={message!r} transcript={transcript_path}")

    # Reboot-resilient fallback: on any hook call, deliver reset notifications
    # whose time has passed in case the detached watcher process didn't survive.
    notify_due_resets(webhook_url, time.time())

    matched, epoch = find_limit_message(message)
    scanned_text = message
    if not matched and transcript_path:
        # Transcript prose is untrusted: only accept an epoch-bearing limit
        # message here, never the loose text-only fallback (which the assistant's
        # own conversation about usage limits would otherwise trip).
        scanned_text = read_transcript_tail(transcript_path)
        matched, epoch = find_limit_message(scanned_text, allow_text_only=False)

    # Recorded unconditionally (not just under CLAUDE_LIMIT_NOTIFIER_DEBUG) so a
    # "no notification arrived" report is diagnosable afterwards even if debug
    # logging wasn't enabled ahead of time. Set CLAUDE_LIMIT_NOTIFIER_LOG_FILE
    # to /dev/null to disable.
    append_debug_log(event_name, scanned_text, matched, epoch)

    if not matched:
        return 0

    dedup_key = str(epoch) if epoch else f"unknown:{time.strftime('%Y-%m-%d')}"
    if already_notified(dedup_key):
        log("already notified for this limit window, skipping")
        return 0

    if not webhook_url:
        print("Detected usage limit but SLACK_WEBHOOK_URL is not set.", file=sys.stderr)
        return 0

    try:
        send_slack_message(webhook_url, build_slack_text(cwd, epoch))
        mark_notified(dedup_key)
        if epoch:
            # Two independent paths deliver the "available again" notice:
            # (1) a detached watcher that fires promptly at the reset time, and
            # (2) a persisted pending-reset record that any later hook call
            #     will honor if the watcher died (reboot/sleep). Both share the
            #     `reset:{epoch}` dedup key so only one message is ever sent.
            add_pending_reset(epoch, cwd)
            spawn_reset_watcher(epoch, cwd)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)

    # Never fail the hook: a non-zero exit here could interfere with Claude Code.
    return 0


if __name__ == "__main__":
    sys.exit(main())
