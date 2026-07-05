#!/usr/bin/env bash
# Initial setup script for claude-limit-noti.
# Supports macOS and Linux (including WSL). Not for native Windows shells
# (cmd/PowerShell) — run this from WSL or Git Bash there instead.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${CLAUDE_LIMIT_NOTI_INSTALL_DIR:-$HOME/.claude/hooks/claude-limit-noti}"
SETTINGS_FILE="${CLAUDE_SETTINGS_FILE:-$HOME/.claude/settings.json}"
HOOK_COMMAND="python3 $INSTALL_DIR/notify_usage_limit.py"

log() { printf '==> %s\n' "$1"; }
warn() { printf 'WARNING: %s\n' "$1" >&2; }
die() { printf 'ERROR: %s\n' "$1" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 1. Detect OS
# ---------------------------------------------------------------------------
OS_NAME="$(uname -s)"
case "$OS_NAME" in
  Darwin)
    OS_KIND="macos"
    DEFAULT_PROFILE="$HOME/.zshrc"
    ;;
  Linux)
    if grep -qi microsoft /proc/version 2>/dev/null; then
      OS_KIND="wsl"
    else
      OS_KIND="linux"
    fi
    DEFAULT_PROFILE="$HOME/.bashrc"
    ;;
  *)
    die "Unsupported OS: $OS_NAME. This script supports macOS and Linux/WSL only."
    ;;
esac
log "Detected OS: $OS_KIND"

# Pick the shell profile file that matches the user's actual login shell,
# falling back to the OS default above if the shell can't be determined.
case "$(basename "${SHELL:-}")" in
  zsh) PROFILE_FILE="$HOME/.zshrc" ;;
  bash)
    if [ "$OS_KIND" = "macos" ]; then
      PROFILE_FILE="$HOME/.bash_profile"
    else
      PROFILE_FILE="$HOME/.bashrc"
    fi
    ;;
  *) PROFILE_FILE="$DEFAULT_PROFILE" ;;
esac

# ---------------------------------------------------------------------------
# 2. Check python3 >= 3.9
# ---------------------------------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
  case "$OS_KIND" in
    macos) HINT="brew install python3" ;;
    linux) HINT="sudo apt install python3   # or: sudo dnf install python3" ;;
    wsl) HINT="sudo apt install python3" ;;
  esac
  die "python3 not found. Install it first: $HINT"
fi

PY_OK=$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 9) else 0)')
if [ "$PY_OK" != "1" ]; then
  die "python3 >= 3.9 is required (found: $(python3 --version 2>&1))."
fi
log "python3 OK: $(python3 --version 2>&1)"

# ---------------------------------------------------------------------------
# 3. Install the script
# ---------------------------------------------------------------------------
mkdir -p "$INSTALL_DIR"
cp "$REPO_DIR/notify_usage_limit.py" "$INSTALL_DIR/notify_usage_limit.py"
chmod +x "$INSTALL_DIR/notify_usage_limit.py"
log "Installed notify_usage_limit.py -> $INSTALL_DIR"

# ---------------------------------------------------------------------------
# 4. Configure SLACK_WEBHOOK_URL
# ---------------------------------------------------------------------------
if [ -z "${SLACK_WEBHOOK_URL:-}" ]; then
  if grep -q '^export SLACK_WEBHOOK_URL=' "$PROFILE_FILE" 2>/dev/null; then
    log "SLACK_WEBHOOK_URL already configured in $PROFILE_FILE, skipping prompt."
    SLACK_WEBHOOK_URL="$(grep '^export SLACK_WEBHOOK_URL=' "$PROFILE_FILE" | tail -1 | cut -d= -f2-)"
    # strip a single layer of surrounding quotes (the export line we write below adds them)
    SLACK_WEBHOOK_URL="${SLACK_WEBHOOK_URL%\"}"
    SLACK_WEBHOOK_URL="${SLACK_WEBHOOK_URL#\"}"
    SLACK_WEBHOOK_URL="${SLACK_WEBHOOK_URL%\'}"
    SLACK_WEBHOOK_URL="${SLACK_WEBHOOK_URL#\'}"
  else
    read -rp "Slack Incoming Webhook URL (https://hooks.slack.com/services/...): " SLACK_WEBHOOK_URL
  fi
fi

case "$SLACK_WEBHOOK_URL" in
  https://hooks.slack.com/services/*) ;;
  *) warn "This doesn't look like a Slack Incoming Webhook URL. Continuing anyway." ;;
esac

if ! grep -q '^export SLACK_WEBHOOK_URL=' "$PROFILE_FILE" 2>/dev/null; then
  {
    echo ""
    echo "# claude-limit-noti"
    echo "export SLACK_WEBHOOK_URL=\"$SLACK_WEBHOOK_URL\""
  } >> "$PROFILE_FILE"
  log "Added SLACK_WEBHOOK_URL to $PROFILE_FILE"
else
  log "$PROFILE_FILE already exports SLACK_WEBHOOK_URL, leaving it as-is."
fi

# ---------------------------------------------------------------------------
# 5. Merge Notification/Stop hooks into settings.json (non-destructive)
# ---------------------------------------------------------------------------
mkdir -p "$(dirname "$SETTINGS_FILE")"
if [ -f "$SETTINGS_FILE" ]; then
  cp "$SETTINGS_FILE" "$SETTINGS_FILE.bak.$(date +%s)"
  log "Backed up existing settings.json"
fi

SETTINGS_FILE="$SETTINGS_FILE" HOOK_COMMAND="$HOOK_COMMAND" python3 - <<'PYEOF'
import json
import os

settings_path = os.environ["SETTINGS_FILE"]
hook_command = os.environ["HOOK_COMMAND"]

try:
    with open(settings_path, encoding="utf-8") as f:
        settings = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    settings = {}

hooks = settings.setdefault("hooks", {})

for event_name in ("Notification", "Stop"):
    groups = hooks.setdefault(event_name, [])
    already_present = any(
        any(h.get("command") == hook_command for h in group.get("hooks", []))
        for group in groups
    )
    if not already_present:
        groups.append({"hooks": [{"type": "command", "command": hook_command}]})

with open(settings_path, "w", encoding="utf-8") as f:
    json.dump(settings, f, indent=2, ensure_ascii=False)
    f.write("\n")

print(f"Merged Notification/Stop hooks into {settings_path}")
PYEOF

# ---------------------------------------------------------------------------
# 6. Test the webhook
# ---------------------------------------------------------------------------
log "Sending a test Slack message..."
if SLACK_WEBHOOK_URL="$SLACK_WEBHOOK_URL" python3 "$INSTALL_DIR/notify_usage_limit.py" --test; then
  log "Setup complete. Check your Slack channel for the test message."
else
  warn "Test message failed to send. Check SLACK_WEBHOOK_URL and try again:"
  warn "  SLACK_WEBHOOK_URL=... python3 $INSTALL_DIR/notify_usage_limit.py --test"
fi

log "Restart your shell (or run: source $PROFILE_FILE) so SLACK_WEBHOOK_URL is available in new sessions."
