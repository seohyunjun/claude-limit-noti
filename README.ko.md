# claude-limit-noti

[English](README.md) | **한국어**

Claude Code(Pro/Max 구독) 사용량 한도에 도달하면 Slack Incoming Webhook으로 알림을 보내는 훅 스크립트입니다.

Claude Code는 사용량 한도에 도달하면 다음과 같은 형태의 메시지를 노출합니다 (내부적으로 비공식 포맷이라 향후 바뀔 수 있습니다):

```
Claude AI usage limit reached|1735700000
```

`notify_usage_limit.py`는 Claude Code의 `Notification` / `Stop` 훅으로 등록되어 이 패턴을 감지하고, 감지 시 Slack 웹훅으로 메시지를 전송합니다. 외부 의존성 없이 Python 표준 라이브러리(`urllib`)만 사용합니다.

## 요구 사항

- Python 3.9 이상
- Slack Incoming Webhook URL ([Slack 앱 설정](https://api.slack.com/messaging/webhooks)에서 발급)

## 설치

### 빠른 설치 (macOS / Linux / WSL)

`install.sh` 하나로 아래 2~4단계(설치, 환경 변수 등록, `settings.json` 훅 병합, 웹훅 테스트)를 자동으로 처리합니다. OS(macOS/Linux/WSL)와 로그인 셸(zsh/bash)을 자동 감지해 알맞은 프로필 파일(`~/.zshrc`, `~/.bashrc`, macOS의 `~/.bash_profile`)에 설정을 추가하고, 기존 `settings.json`의 다른 훅/설정은 그대로 두고 필요한 항목만 병합합니다(실행 전 자동 백업). 여러 번 실행해도 중복 등록되지 않습니다. 네이티브 Windows(cmd/PowerShell)에서는 지원하지 않으며, WSL이나 Git Bash에서 실행하세요.

```bash
git clone https://github.com/seohyunjun/claude-limit-noti
cd claude-limit-noti
./install.sh
```

Slack 웹훅 URL을 물어보면 입력하세요(이미 환경 변수로 설정돼 있으면 건너뜁니다). 완료되면 실제 Slack 채널로 테스트 메시지가 전송됩니다.

### 수동 설치

1. 이 저장소를 클론하거나 `notify_usage_limit.py`를 원하는 위치에 저장합니다.
   ```bash
   git clone https://github.com/seohyunjun/claude-limit-noti ~/.claude/hooks/claude-limit-noti
   chmod +x ~/.claude/hooks/claude-limit-noti/notify_usage_limit.py
   ```
2. Slack 웹훅 URL을 환경 변수로 설정합니다 (쉘 프로필에 추가 권장: `~/.bashrc`, `~/.zshrc` 등).
   ```bash
   export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/XXX/YYY/ZZZ"
   ```
3. Claude Code 설정 파일(`~/.claude/settings.json` 또는 프로젝트 `.claude/settings.json`)에 훅을 등록합니다. `settings.example.json`을 참고하세요.
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
   > **주의**: settings.json을 GitHub에 공개로 올릴 경우, 웹훅 URL을 커맨드에 직접 박아넣지 말고 위 예시처럼 환경 변수(`SLACK_WEBHOOK_URL`)를 셸 프로필에서 읽도록 하세요. 훅 커맨드는 로그인 셸을 통해 실행되므로 export한 환경 변수를 그대로 사용할 수 있습니다.

4. 설정을 테스트합니다.
   ```bash
   SLACK_WEBHOOK_URL="https://hooks.slack.com/services/XXX/YYY/ZZZ" \
     python3 notify_usage_limit.py --test
   ```
   Slack 채널에 테스트 메시지가 오면 웹훅 연결은 정상 동작하는 것입니다.
   > **참고**: `--test`가 표시하는 재설정 시각은 실제 사용량 한도와 무관한 "지금+1시간" 더미값입니다. 웹훅 연결만 확인하는 용도이며, 실제 한도 도달 시 전송되는 메시지의 재설정 시각은 Claude Code가 전달하는 실제 epoch 값을 기준으로 계산됩니다.

## 동작 방식

- `Notification` 훅 입력(JSON, stdin)의 `message` 필드에서 한도 도달 패턴을 검사합니다.
- 매칭되지 않으면 `transcript_path`로 전달된 세션 트랜스크립트(JSONL)의 최근 메시지들도 함께 검사합니다 (한도 메시지가 Notification이 아닌 트랜스크립트 내부에 남는 경우 대비).
- `Claude AI usage limit reached|<epoch>` 형태가 매칭되면 epoch을 사람이 읽을 수 있는 재설정 시각으로 변환해 Slack 메시지에 포함합니다.
- 동일한 한도 윈도우(같은 epoch)에 대해 중복 알림을 보내지 않도록 `~/.claude/usage-limit-notifier-state.json`에 알림을 보낸 키 목록을 저장합니다.
- 훅 스크립트는 항상 exit code 0으로 종료합니다. Claude Code 동작을 절대 막지 않기 위함입니다.
- **한도 도달을 감지하면, 재설정 시각까지 기다렸다가 "이제 다시 사용할 수 있어요" 메시지를 추가로 보내는 별도 프로세스**를 백그라운드로 하나 띄웁니다(내부적으로 자기 자신을 `--wait-reset <epoch>` 인자로 재실행). 이 프로세스는 Claude Code나 훅을 호출한 부모 프로세스가 끝나도 계속 살아있지만, **컴퓨터가 꺼지거나 잠자기 모드에 들어가면 함께 종료**됩니다.
- 위 워처가 죽는 경우(재부팅·절전)를 대비해, 재설정 시각(epoch)은 상태 파일에도 저장됩니다. 이후 **아무 훅이나 다시 호출되면**, 재설정 시각이 이미 지난 예약 건을 그때 발송합니다(중복은 `reset:<epoch>` 키로 방지). 즉 재부팅 후 Claude Code를 다시 쓰는 순간 알림을 놓치지 않습니다.
- 나아가 **Claude Code 실행 여부와 무관하게** 알림을 받으려면, OS 스케줄러(cron/systemd/launchd)에 `--check-resets` 모드를 주기적으로 등록합니다. 이 모드는 stdin을 읽지 않고 상태 파일만 보고 지난 재설정 건을 발송합니다. `install.sh`가 OS에 맞게 자동 등록합니다([아래](#재부팅에도-견디는-재설정-알림-os-스케줄러) 참고).
- 훅이 호출될 때마다(매칭 여부와 상관없이) `~/.claude/usage-limit-notifier-debug.log`에 이벤트 이름·매칭 여부·검사한 메시지 일부(최대 300자)를 한 줄씩 남깁니다. 최근 200건만 유지되며(그 이상은 자동으로 잘림), "알림이 안 왔다"는 문제를 사후에 진단하기 위한 용도입니다. 끄고 싶으면 `CLAUDE_LIMIT_NOTIFIER_LOG_FILE=/dev/null`로 설정하세요.

## 문제 해결 — 실제 한도 도달인데 알림이 안 왔다면

1. `~/.claude/usage-limit-notifier-debug.log`를 확인하세요.
   - **파일 자체가 없거나 해당 시각에 기록이 없다면**: 훅이 아예 호출되지 않은 것입니다. `~/.claude/settings.json`(또는 프로젝트 `.claude/settings.json`)에 `Notification`/`Stop` 훅이 실제로 등록돼 있는지, 사용 중인 머신/디렉토리에도 설치했는지 확인하세요.
   - **기록은 있는데 `"matched": false`라면**: 훅은 호출됐지만 실제 메시지가 `LIMIT_PATTERNS`와 매칭되지 않은 것입니다. `message_snippet` 필드에 찍힌 실제 텍스트를 확인해서, 필요하면 스크립트 상단 `LIMIT_PATTERNS` 정규식을 그 텍스트에 맞게 수정하세요. `Claude AI usage limit reached|<epoch>` 포맷은 Claude Code의 비공식 내부 표기라 실제 문구가 다를 수 있습니다.
   - **`"matched": true`인데 Slack에 안 왔다면**: `SLACK_WEBHOOK_URL`이 그 훅 실행 환경에 제대로 노출되지 않았을 가능성이 큽니다(예: 훅이 로그인 셸을 거치지 않는 환경). `CLAUDE_LIMIT_NOTIFIER_DEBUG=1`을 켜고 stderr 로그도 함께 확인하세요.
2. 어떤 상황인지 확인했으면 [이슈](https://github.com/seohyunjun/claude-limit-noti/issues)로 `message_snippet` 내용과 함께 알려주시면 패턴을 반영하겠습니다(민감한 정보는 가려주세요).

## 환경 변수

| 변수 | 필수 | 설명 |
|---|---|---|
| `SLACK_WEBHOOK_URL` | 예 | Slack Incoming Webhook URL |
| `CLAUDE_LIMIT_NOTIFIER_TZ` | 아니오 | 재설정 시각 표시에 사용할 IANA 타임존 (예: `Asia/Seoul`). 미설정 시 시스템 로컬 타임존 사용 |
| `CLAUDE_LIMIT_NOTIFIER_STATE_FILE` | 아니오 | 중복 알림 방지용 상태 파일 경로 (기본값: `~/.claude/usage-limit-notifier-state.json`) |
| `CLAUDE_LIMIT_NOTIFIER_DEBUG` | 아니오 | 값이 설정되면 stderr에 디버그 로그 출력 |
| `CLAUDE_LIMIT_NOTIFIER_LOG_FILE` | 아니오 | 모든 훅 호출 기록을 남기는 로그 파일 경로 (기본값: `~/.claude/usage-limit-notifier-debug.log`). `/dev/null`로 설정하면 비활성화 |

## 재부팅에도 견디는 재설정 알림 (OS 스케줄러)

"이제 다시 사용할 수 있어요" 알림을 **재부팅·절전과 무관하게, 그리고 Claude Code를 다시 켜지 않아도** 받으려면, `--check-resets` 모드를 OS 스케줄러에 주기적으로(예: 10분마다) 등록하면 됩니다. 이 모드는 상태 파일에 저장된 예약 재설정 중 시각이 지난 것을 발송하고, 이미 보낸 건은 다시 보내지 않습니다.

`install.sh`는 OS를 감지해 아래를 **자동으로 등록**합니다(스킵하려면 `CLAUDE_LIMIT_NOTI_NO_SCHEDULER=1 ./install.sh`). 스케줄러는 로그인 셸 프로필을 거치지 않으므로, 설치 스크립트가 `SLACK_WEBHOOK_URL`(및 설정된 경우 `CLAUDE_LIMIT_NOTIFIER_TZ`/`CLAUDE_LIMIT_NOTIFIER_STATE_FILE`)을 스케줄러 정의에 직접 넣어 줍니다.

- **macOS — launchd**: `~/Library/LaunchAgents/com.claude-limit-noti.reset-checker.plist`를 생성하고 `launchctl load`로 등록합니다. 로그인 시(`RunAtLoad`)와 10분마다(`StartInterval`) 실행됩니다.
  - 해제: `launchctl unload ~/Library/LaunchAgents/com.claude-limit-noti.reset-checker.plist && rm "$_"`
- **Linux — systemd user timer**: `systemctl --user`가 동작하면 `claude-limit-noti-reset.timer`(부팅 2분 후 + 이후 10분마다)를 등록합니다. 로그아웃 상태에서도 돌게 하려면 linger가 필요합니다: `sudo loginctl enable-linger $USER`(스크립트가 자동 시도).
  - 해제: `systemctl --user disable --now claude-limit-noti-reset.timer`
- **Linux/WSL — cron 폴백**: systemd user 인스턴스를 못 쓰면 crontab에 10분 간격 항목을 넣습니다(중복 방지 마커 포함).
  - **WSL 주의**: cron 데몬은 부팅 시 자동 실행되지 않습니다. `sudo service cron start`로 켜고, 부팅 시 자동 시작되도록 별도 설정하세요. 그렇지 않으면 cron이 떠 있는 동안에만 동작합니다.
  - 해제: `crontab -e`에서 `claude-limit-noti reset-checker` 주석이 달린 줄 삭제
- **네이티브 Windows — 작업 스케줄러**: `install.sh`는 네이티브 Windows 셸을 지원하지 않으므로 수동으로 등록합니다(PowerShell, 경로는 실제 설치 위치로 교체). 웹훅 URL은 사용자 환경 변수로 저장해 두는 것을 권장합니다(`setx SLACK_WEBHOOK_URL "https://hooks.slack.com/services/XXX/YYY/ZZZ"`).
  ```powershell
  $py = (Get-Command python).Source
  $script = "$HOME\.claude\hooks\claude-limit-noti\notify_usage_limit.py"
  $action  = New-ScheduledTaskAction -Execute $py -Argument "`"$script`" --check-resets"
  $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
             -RepetitionInterval (New-TimeSpan -Minutes 10)
  Register-ScheduledTask -TaskName "claude-limit-noti reset-checker" `
    -Action $action -Trigger $trigger -Description "Claude 사용량 재설정 알림 체크"
  ```
  - 해제: `Unregister-ScheduledTask -TaskName "claude-limit-noti reset-checker" -Confirm:$false`

> 참고: 어떤 방식이든 10분 간격이면 재설정 알림이 최대 10분 늦게 올 수 있습니다(간격은 취향대로 조정하세요). 스케줄러를 쓰더라도 한도 **도달** 알림은 그대로 Claude Code 훅이 담당합니다.

## 제한 사항

- `Claude AI usage limit reached|<epoch>` 포맷은 Claude Code의 비공식/미문서화된 내부 표기입니다. Claude Code 업데이트로 포맷이 바뀌면 정규식(`LIMIT_PATTERNS`)이 더 이상 매칭하지 않을 수 있습니다. 이 경우 스크립트 상단의 `LIMIT_PATTERNS`를 실제 관측된 메시지에 맞게 수정하세요.
- 텍스트 전용 폴백 패턴(`usage limit reached` 등)은 매칭되어도 정확한 재설정 시각을 알 수 없고, 이 경우 재설정 알림용 워처도 뜨지 않습니다(기다릴 정확한 시각을 모르기 때문).
- 재설정 알림 워처는 백그라운드 OS 프로세스일 뿐이라, **컴퓨터가 꺼지거나 절전 모드에 들어가면 그 프로세스는 종료됩니다.** 다만 재설정 시각이 상태 파일에도 저장되므로, 이후 훅이 다시 호출되거나 OS 스케줄러(`--check-resets`, [위 섹션](#재부팅에도-견디는-재설정-알림-os-스케줄러) 참고)가 돌면 놓친 알림을 뒤늦게라도 발송합니다. 스케줄러까지 등록하면 재부팅·절전과 무관하게 받을 수 있습니다.
- 재설정 시각이 지금부터 **1시간 이상 과거**이거나 **8일 이상 미래**면(잘못 파싱된 epoch 등 이상값 방지용 안전장치), 워처를 아예 띄우지 않고 조용히 건너뜁니다. 정상적인 5시간/주간 한도라면 이 범위에 걸릴 일은 없습니다.

## 테스트

외부 의존성 없이 표준 라이브러리 `unittest`만으로 작성되어 있습니다. 저장소 루트에서 실행하세요.

```bash
python3 -m unittest discover -s tests -v
```

패턴 감지, 타임존 변환, 중복 알림 방지, Slack 전송(mock), 재설정 워처의 스폰 범위 검사는 물론, 실제로
로컬 mock Slack 서버를 띄우고 스크립트를 subprocess로 구동해 감지→중복방지→알림→재설정 워처까지
전체 파이프라인을 검증하는 end-to-end 테스트도 포함되어 있습니다.

## 라이선스

[MIT](LICENSE)
