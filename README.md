# Claude Code Usage Monitor

macOS 메뉴바에서 [Claude Code](https://docs.anthropic.com/en/docs/claude-code) 사용량을 실시간으로 보여주는 앱입니다. `/usage` 명령어와 동일한 데이터를 항상 확인할 수 있습니다.

![menubar](https://img.shields.io/badge/macOS-menu%20bar-blue)

## 기능

- **실시간 사용량** — Anthropic API 연동 (`/usage`와 동일한 데이터)
- 세션(5h), 주간(전 모델), 주간(Sonnet) 프로그레스 바
- 토큰 및 비용 상세 내역 (로컬 JSONL 로그 기반)
- 모델별 사용량 통계
- **사용량 임계치 알림** — 80%, 90% 도달 시 macOS 알림 (세션 + 주간)
- **7일 사용량 히스토리** — 스파크라인 차트(▁▃▅▇▅▂▁) + 일별 비용 상세
- **OAuth 토큰 자동 갱신** — 밤새 토큰 만료 시 refresh token으로 자동 갱신
- **자동 업데이트 확인** — 1시간마다 GitHub releases 확인, 새 버전 알림
- 5분 간격 자동 새로고침
- 로그인 시 자동 시작 (선택)

## 요구사항

- macOS (Apple Silicon)
- Python 3
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) 설치 및 로그인 완료

## 설치

### 방법 1: 소스 (git clone)

```bash
git clone https://github.com/hmyanghm/claude-usage-menubar.git
cd claude-usage-menubar
./setup.sh
```

### 방법 2: DMG

[Releases](https://github.com/hmyanghm/claude-usage-menubar/releases/latest)에서 최신 `.dmg`를 다운로드하여 Applications에 드래그하세요.

## 실행

```bash
~/.claude-menubar/launch.sh
```

## 로그인 시 자동 시작

```bash
launchctl load ~/Library/LaunchAgents/com.claude.usage-monitor.plist
```

## 업데이트

| 설치 방법 | 업데이트 방법 |
|-----------|---------------|
| **Git clone** | `cd claude-usage-menubar && git pull` |
| **DMG** | [Releases](https://github.com/hmyanghm/claude-usage-menubar/releases/latest)에서 새 `.dmg` 다운로드 |

두 방법 모두 메뉴바에서 동일한 업데이트 알림을 받습니다. "🔔 업데이트 사용 가능"을 클릭하면 릴리스 페이지가 열립니다.

## 삭제

```bash
launchctl unload ~/Library/LaunchAgents/com.claude.usage-monitor.plist 2>/dev/null
rm -rf ~/.claude-menubar
rm -f ~/Library/LaunchAgents/com.claude.usage-monitor.plist
```

## 동작 원리

macOS Keychain에서 Claude Code OAuth 토큰을 읽어 Anthropic Usage API를 호출합니다. 토큰/비용 상세는 `~/.claude/`의 로컬 JSONL 세션 파일에서 계산합니다. API 장애 시 로컬 추정치로 폴백합니다. OAuth 토큰이 만료되면 refresh token으로 자동 갱신합니다.
