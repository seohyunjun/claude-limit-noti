# claude-limit-noti

**English** | [한국어](README.ko.md)

A Claude Code hook script that sends a Slack Incoming Webhook notification when your Claude Code (Pro/Max subscription) usage limit is reached.

When the usage limit is hit, Claude Code surfaces a message in a form like the following (this is an unofficial internal format and may change in the future):

```
Claude AI usage limit reached|1735700000
```

`notify_usage_limit.py` is registered as a Claude Code `Notification` / `Stop` hook, detects this pattern, and posts a message to a Slack webhook when it fires. It has no external dependencies — only the Python standard library (`urllib`).

## Requirements

- Python 3.9 or newer
- A Slack Incoming Webhook URL (create one from [Slack app settings](https://api.slack.com/messaging/webhooks))

## Installation

### Quick install (macOS / Linux / WSL)

A single `install.sh` automates steps 2–4 below (install, register the environment variable, merge the `settings.json` hooks, and test the webhook). It auto-detects your OS (macOS/Linux/WSL) and login shell (zsh/bash) and adds the configuration to the appropriate profile file (`~/.zshrc`, `~/.bashrc`, or `~/.bash_profile` on macOS). It leaves any other hooks/settings in your existing `settings.json` untouched and only merges in what's needed (with an automatic backup before it runs). Running it multiple times will not create duplicate registrations. Native Windows (cmd/PowerShell) is not supported — run it under WSL or Git Bash.

```bash
git clone https://github.com/seohyunjun/claude-limit-noti
cd claude-limit-noti
./install.sh
```

When prompted, enter your Slack webhook URL (it is skipped if already set as an environment variable). Once finished, a test message is sent to your actual Slack channel.

### Manual install

1. Clone this repository or save `notify_usage_limit.py` wherever you like.
   ```bash
   git clone https://github.com/seohyunjun/claude-limit-noti ~/.claude/hooks/claude-limit-noti
   chmod +x ~/.claude/hooks/claude-limit-noti/notify_usage_limit.py
   ```
2. Set your Slack webhook URL as an environment variable (adding it to your shell profile is recommended: `~/.bashrc`, `~/.zshrc`, etc.).
   ```bash
   export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/XXX/YYY/ZZZ"
   ```
3. Register the hook in your Claude Code settings file (`~/.claude/settings.json` or the project-level `.claude/settings.json`). See `settings.example.json` for reference.
   ```json
   {
     "hooks": {
       "Notification": [
         { "hooks": [{ "type": "command", "command": "python3 ~/.claude/hooks/claude-limit-noti/notify_usage_limit.py" }] }
       ],
       "Stop": [
         { "hooks": [{ "type": "command", "command": "python3 ~/.claude/hooks/claude-limit-noti/notify_usage_limit.py" }] }
       ]
     }
   }
   ```
   > **Note**: If you publish your `settings.json` publicly on GitHub, do not hard-code the webhook URL into the command. Instead, read it from an environment variable (`SLACK_WEBHOOK_URL`) in your shell profile as shown above. Hook commands run through your login shell, so exported environment variables are available as-is.

4. Test the setup.
   ```bash
   SLACK_WEBHOOK_URL="https://hooks.slack.com/services/XXX/YYY/ZZZ" \
     python3 notify_usage_limit.py --test
   ```
   If the test message arrives in your Slack channel, the webhook connection is working.
   > **Note**: The reset time shown by `--test` is a dummy "now + 1 hour" value unrelated to any real usage limit. It only verifies the webhook connection. When a real limit is reached, the reset time in the message is computed from the actual epoch value Claude Code provides.

## How it works

- It checks the `message` field of the `Notification` hook input (JSON over stdin) for the limit-reached pattern.
- If there's no match, it also scans the recent messages of the session transcript (JSONL) provided via `transcript_path`, in case the limit message ends up inside the transcript rather than in the Notification.
- When a `Claude AI usage limit reached|<epoch>` form matches, it converts the epoch into a human-readable reset time and includes it in the Slack message.
- To avoid sending duplicate notifications for the same limit window (same epoch), it stores the list of already-notified keys in `~/.claude/usage-limit-notifier-state.json`.
- The hook script always exits with code 0, so that it never blocks Claude Code's operation.
- **When it detects a limit being reached, it spawns one separate background process that waits until the reset time and then sends an additional "you can use it again now" message** (internally re-invoking itself with the `--wait-reset <epoch>` argument). This process keeps running even after Claude Code (or the parent process that invoked the hook) exits, but **it is terminated if the computer is shut down or goes to sleep**.
- To guard against that watcher dying (reboot/sleep), the reset time (epoch) is also persisted to the state file. Afterwards, **on any subsequent hook invocation**, it sends any scheduled reset whose time has already passed (duplicates are prevented by the `reset:<epoch>` key). In other words, the moment you use Claude Code again after a reboot, you won't miss the notification.
- Furthermore, to receive the notification **regardless of whether Claude Code is running**, register the `--check-resets` mode on an OS scheduler (cron/systemd/launchd) to run periodically. This mode does not read stdin — it only looks at the state file and sends any past-due resets. `install.sh` registers this automatically for your OS (see [below](#reboot-resilient-reset-notifications-os-scheduler)).
- Every time the hook is invoked (regardless of whether it matched), it appends one line to `~/.claude/usage-limit-notifier-debug.log` recording the event name, whether it matched, and a portion of the scanned message (up to 300 chars). Only the most recent 200 entries are kept (older ones are trimmed automatically). This exists to diagnose "the notification never arrived" reports after the fact. To disable it, set `CLAUDE_LIMIT_NOTIFIER_LOG_FILE=/dev/null`.

## Troubleshooting — a real limit was reached but no notification arrived

1. Check `~/.claude/usage-limit-notifier-debug.log`.
   - **If the file itself doesn't exist, or there's no entry around that time**: the hook was never invoked. Verify that the `Notification`/`Stop` hooks are actually registered in `~/.claude/settings.json` (or the project `.claude/settings.json`), and that you installed it on the machine/directory you were using.
   - **If there is an entry but `"matched": false`**: the hook was invoked, but the actual message did not match `LIMIT_PATTERNS`. Look at the real text captured in the `message_snippet` field and, if needed, adjust the `LIMIT_PATTERNS` regexes at the top of the script to fit that text. The `Claude AI usage limit reached|<epoch>` format is Claude Code's unofficial internal notation, so the actual wording may differ.
   - **If `"matched": true` but nothing arrived in Slack**: most likely `SLACK_WEBHOOK_URL` was not properly exposed to that hook's execution environment (e.g. a hook that doesn't go through the login shell). Turn on `CLAUDE_LIMIT_NOTIFIER_DEBUG=1` and check the stderr logs as well.
2. Once you've figured out which situation it is, please open an [issue](https://github.com/seohyunjun/claude-limit-noti/issues) with the `message_snippet` content and I'll incorporate the pattern (please redact any sensitive information).

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `SLACK_WEBHOOK_URL` | Yes | Slack Incoming Webhook URL |
| `CLAUDE_LIMIT_NOTIFIER_TZ` | No | IANA timezone used to display the reset time (e.g. `Asia/Seoul`). Falls back to the system local timezone if unset |
| `CLAUDE_LIMIT_NOTIFIER_STATE_FILE` | No | Path to the dedup state file (default: `~/.claude/usage-limit-notifier-state.json`) |
| `CLAUDE_LIMIT_NOTIFIER_DEBUG` | No | If set to any value, prints debug logs to stderr |
| `CLAUDE_LIMIT_NOTIFIER_LOG_FILE` | No | Path to the log file that records every hook invocation (default: `~/.claude/usage-limit-notifier-debug.log`). Set to `/dev/null` to disable |

## Reboot-resilient reset notifications (OS scheduler)

To receive the "you can use it again now" notification **regardless of reboots/sleep, and without having to reopen Claude Code**, register the `--check-resets` mode on an OS scheduler to run periodically (e.g. every 10 minutes). This mode sends any scheduled resets stored in the state file whose time has passed, and does not re-send ones that were already sent.

`install.sh` detects your OS and **registers the following automatically** (to skip it, run `CLAUDE_LIMIT_NOTI_NO_SCHEDULER=1 ./install.sh`). Since schedulers do not go through your login shell profile, the install script embeds `SLACK_WEBHOOK_URL` (and, if set, `CLAUDE_LIMIT_NOTIFIER_TZ` / `CLAUDE_LIMIT_NOTIFIER_STATE_FILE`) directly into the scheduler definition.

- **macOS — launchd**: creates `~/Library/LaunchAgents/com.claude-limit-noti.reset-checker.plist` and registers it with `launchctl load`. It runs at login (`RunAtLoad`) and every 10 minutes (`StartInterval`).
  - Uninstall: `launchctl unload ~/Library/LaunchAgents/com.claude-limit-noti.reset-checker.plist && rm "$_"`
- **Linux — systemd user timer**: if `systemctl --user` works, it registers `claude-limit-noti-reset.timer` (2 minutes after boot + every 10 minutes thereafter). Running while logged out requires linger: `sudo loginctl enable-linger $USER` (the script attempts this automatically).
  - Uninstall: `systemctl --user disable --now claude-limit-noti-reset.timer`
- **Linux/WSL — cron fallback**: if a systemd user instance is unavailable, it adds a 10-minute-interval entry to your crontab (with a dedup marker).
  - **WSL note**: the cron daemon does not start automatically at boot. Start it with `sudo service cron start`, and set it up to start automatically at boot separately. Otherwise it only works while cron happens to be running.
  - Uninstall: delete the line commented `claude-limit-noti reset-checker` via `crontab -e`
- **Native Windows — Task Scheduler**: `install.sh` does not support native Windows shells, so register it manually (PowerShell; replace the path with your actual install location). Storing the webhook URL as a user environment variable is recommended (`setx SLACK_WEBHOOK_URL "https://hooks.slack.com/services/XXX/YYY/ZZZ"`).
  ```powershell
  $py = (Get-Command python).Source
  $script = "$HOME\.claude\hooks\claude-limit-noti\notify_usage_limit.py"
  $action  = New-ScheduledTaskAction -Execute $py -Argument "`"$script`" --check-resets"
  $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
             -RepetitionInterval (New-TimeSpan -Minutes 10)
  Register-ScheduledTask -TaskName "claude-limit-noti reset-checker" `
    -Action $action -Trigger $trigger -Description "Check Claude usage reset notifications"
  ```
  - Uninstall: `Unregister-ScheduledTask -TaskName "claude-limit-noti reset-checker" -Confirm:$false`

> Note: with any approach, a 10-minute interval means the reset notification may arrive up to 10 minutes late (adjust the interval to taste). Even with a scheduler, the limit-**reached** notification is still handled by the Claude Code hook.

## Limitations

- The `Claude AI usage limit reached|<epoch>` format is Claude Code's unofficial/undocumented internal notation. If a Claude Code update changes the format, the regexes (`LIMIT_PATTERNS`) may no longer match. In that case, update `LIMIT_PATTERNS` at the top of the script to fit the actually-observed message.
- Even when the text-only fallback patterns (`usage limit reached`, etc.) match, the exact reset time is unknown, and in that case the reset-notification watcher is not spawned either (since it has no exact time to wait for).
- The reset-notification watcher is just a background OS process, so **it is terminated if the computer is shut down or goes to sleep.** However, because the reset time is also stored in the state file, a later hook invocation or the OS scheduler (`--check-resets`, see the [section above](#reboot-resilient-reset-notifications-os-scheduler)) will send the missed notification belatedly. Registering the scheduler on top of that lets you receive it regardless of reboots/sleep.
- If the reset time is **more than 1 hour in the past** or **more than 8 days in the future** (a safety guard against anomalous values such as a mis-parsed epoch), the watcher is skipped silently rather than spawned. For normal 5-hour/weekly limits, you won't hit this range.

## Tests

Written with only the standard-library `unittest`, with no external dependencies. Run from the repository root.

```bash
python3 -m unittest discover -s tests -v
```

Beyond pattern detection, timezone conversion, duplicate-notification prevention, Slack delivery (mocked), and the reset watcher's spawn-range checks, it also includes an end-to-end test that actually starts a local mock Slack server and drives the script as a subprocess, verifying the entire pipeline: detection → deduplication → notification → reset watcher.

## License

[MIT](LICENSE)
