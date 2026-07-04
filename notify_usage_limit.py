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

# Order matters: patterns with a captured epoch timestamp are tried first so
# we can show an exact reset time; the last entry is a text-only fallback in
# case Claude Code changes the machine-readable format.
LIMIT_PATTERNS = [
    re.compile(r"claude\s*(ai)?\s*usage limit reached\|(\d+)", re.IGNORECASE),
    re.compile(r"usage limit reached\|(\d+)", re.IGNORECASE),
    re.compile(
        r"(usage limit reached|5-hour limit reached|weekly limit reached|limit will reset)",
        re.IGNORECASE,
    ),
]


def log(*args):
    if os.environ.get("CLAUDE_LIMIT_NOTIFIER_DEBUG"):
        print("[usage-limit-notifier]", *args, file=sys.stderr)


def find_limit_message(text: str):
    """Return (matched, reset_epoch_or_None) for the first pattern that hits."""
    if not text:
        return False, None
    for pattern in LIMIT_PATTERNS:
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
    return load_state().get("last_dedup_key") == dedup_key


def mark_notified(dedup_key: str) -> None:
    save_state({"last_dedup_key": dedup_key, "notified_at": time.time()})


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


def build_slack_text(cwd: str, reset_epoch) -> str:
    lines = [":warning: *Claude Code 사용량 한도 도달*"]
    if cwd:
        lines.append(f"- 프로젝트 경로: `{cwd}`")
    if reset_epoch:
        lines.append(f"- 재설정 예정: `{format_reset_time(reset_epoch)}`")
    now_str = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    lines.append(f"- 감지 시각: `{now_str}`")
    return "\n".join(lines)


def main() -> int:
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")

    if "--test" in sys.argv:
        if not webhook_url:
            print("SLACK_WEBHOOK_URL is not set.", file=sys.stderr)
            return 1
        send_slack_message(webhook_url, build_slack_text("(test)", int(time.time()) + 3600))
        print("Test message sent.")
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

    matched, epoch = find_limit_message(message)
    if not matched and transcript_path:
        matched, epoch = find_limit_message(read_transcript_tail(transcript_path))

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
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)

    # Never fail the hook: a non-zero exit here could interfere with Claude Code.
    return 0


if __name__ == "__main__":
    sys.exit(main())
