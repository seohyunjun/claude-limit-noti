# claude-limit-noti

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

## 환경 변수

| 변수 | 필수 | 설명 |
|---|---|---|
| `SLACK_WEBHOOK_URL` | 예 | Slack Incoming Webhook URL |
| `CLAUDE_LIMIT_NOTIFIER_TZ` | 아니오 | 재설정 시각 표시에 사용할 IANA 타임존 (예: `Asia/Seoul`). 미설정 시 시스템 로컬 타임존 사용 |
| `CLAUDE_LIMIT_NOTIFIER_STATE_FILE` | 아니오 | 중복 알림 방지용 상태 파일 경로 (기본값: `~/.claude/usage-limit-notifier-state.json`) |
| `CLAUDE_LIMIT_NOTIFIER_DEBUG` | 아니오 | 값이 설정되면 stderr에 디버그 로그 출력 |

## 제한 사항

- `Claude AI usage limit reached|<epoch>` 포맷은 Claude Code의 비공식/미문서화된 내부 표기입니다. Claude Code 업데이트로 포맷이 바뀌면 정규식(`LIMIT_PATTERNS`)이 더 이상 매칭하지 않을 수 있습니다. 이 경우 스크립트 상단의 `LIMIT_PATTERNS`를 실제 관측된 메시지에 맞게 수정하세요.
- 텍스트 전용 폴백 패턴(`usage limit reached` 등)은 매칭되어도 정확한 재설정 시각을 알 수 없고, 이 경우 재설정 알림용 워처도 뜨지 않습니다(기다릴 정확한 시각을 모르기 때문).
- 재설정 알림 워처는 백그라운드 OS 프로세스일 뿐이라, **컴퓨터가 꺼지거나 절전 모드에 들어가면 알림이 오지 않습니다.** 노트북을 덮거나 서버를 재시작하는 사이에 한도가 풀리는 경우가 잦다면 이 방식만으로는 놓칠 수 있습니다.
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
