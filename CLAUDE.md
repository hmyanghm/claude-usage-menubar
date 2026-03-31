# Claude Usage Monitor

## Overview
macOS 메뉴바 / Windows 시스템 트레이에서 Claude Code 사용량을 실시간으로 보여주는 앱 (v1.0.9)

## Tech Stack
- Python 3 + rumps (macOS menubar framework)
- Anthropic OAuth API (usage/profile)
- macOS Keychain (OAuth token 저장)
- PyInstaller (.app/.dmg 빌드)

## Build & Run
- 설치: `./setup.sh`
- 실행: `~/.claude-menubar/launch.sh`
- 빌드: `./build_dmg.sh` (PyInstaller → .app → .dmg)
- 자동시작: `launchctl load ~/Library/LaunchAgents/com.claude.usage-monitor.plist`

## Directory Structure
```
claude_menubar.py          # 메인 앱 (단일 파일)
setup.sh                   # 설치 스크립트
build_dmg.sh               # PyInstaller 빌드 → DMG
Claude Usage Monitor.spec  # PyInstaller 설정
app_icon.icns              # 앱 아이콘
```

## Key Architecture
- `ClaudeUsageApp(rumps.App)`: 메뉴바 앱 메인 클래스
- `UsageTracker`: ~/.claude/ JSONL 파일 파싱 및 토큰/비용 집계
- `fetch_usage_api()`: Anthropic OAuth API 호출 (캐시/백오프 포함)
- 설정 파일: `~/.claude/menubar_config.json`

## Conventions
- 한국어 UI, 영어 코드/주석
- 단일 파일 구조 (claude_menubar.py)
- 커밋 메시지: `<type>: <한국어 설명>`
