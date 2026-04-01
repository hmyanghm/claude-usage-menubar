# Claude Code Usage Monitor

macOS 메뉴바 / Windows 시스템 트레이에서 [Claude Code](https://docs.anthropic.com/en/docs/claude-code) 사용량을 실시간으로 보여주는 앱입니다. `/usage` 명령어와 동일한 데이터를 항상 확인할 수 있습니다.

![macOS](https://img.shields.io/badge/macOS-menu%20bar-blue) ![Windows](https://img.shields.io/badge/Windows-system%20tray-0078D6)

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

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) 설치 및 로그인 완료
- **macOS**: macOS (Apple Silicon), Python 3
- **Windows**: Windows 10/11, Python 3.8+

---

## macOS

### 설치

#### 방법 1: 소스 (git clone)

```bash
git clone https://github.com/hmyanghm/claude-usage-menubar.git
cd claude-usage-menubar
./setup.sh
```

#### 방법 2: DMG

[Releases](https://github.com/hmyanghm/claude-usage-menubar/releases/latest)에서 최신 `.dmg`를 다운로드하여 Applications에 드래그하세요.

### 실행

```bash
~/.claude-menubar/launch.sh
```

### 로그인 시 자동 시작

```bash
launchctl load ~/Library/LaunchAgents/com.claude.usage-monitor.plist
```

### 업데이트

| 설치 방법 | 업데이트 방법 |
|-----------|---------------|
| **Git clone** | `cd claude-usage-menubar && git pull` |
| **DMG** | [Releases](https://github.com/hmyanghm/claude-usage-menubar/releases/latest)에서 새 `.dmg` 다운로드 |

두 방법 모두 메뉴바에서 동일한 업데이트 알림을 받습니다. "🔔 업데이트 사용 가능"을 클릭하면 릴리스 페이지가 열립니다.

### 삭제

```bash
launchctl unload ~/Library/LaunchAgents/com.claude.usage-monitor.plist 2>/dev/null
rm -rf ~/.claude-menubar
rm -f ~/Library/LaunchAgents/com.claude.usage-monitor.plist
```

---

## Windows

### 설치

```cmd
git clone https://github.com/hmyanghm/claude-usage-menubar.git
cd claude-usage-menubar
setup_windows.bat
```

설치 스크립트가 자동으로:
1. Python 의존성 설치 (`pystray`, `Pillow`)
2. `%USERPROFILE%\.claude-menubar\`에 파일 복사
3. (선택) Windows 시작프로그램에 등록

### 실행

```cmd
%USERPROFILE%\.claude-menubar\launch.bat
```

또는 직접 실행:

```cmd
python claude_menubar_windows.py
```

### 삭제

```cmd
rmdir /s /q %USERPROFILE%\.claude-menubar
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Claude Usage Monitor.lnk"
```

### 트러블슈팅

OAuth 토큰을 `~/.claude/.credentials.json` 파일에서 우선 읽고, 없으면 Windows 자격 증명 관리자를 fallback으로 사용합니다. Claude Code 로그인 후에도 토큰을 찾지 못하는 경우:

1. **인증 파일 확인**: `%USERPROFILE%\.claude\.credentials.json` 파일이 존재하는지 확인
2. **자격 증명 관리자 확인**: 제어판 → 자격 증명 관리자 → Windows 자격 증명 → "Claude" 관련 항목 확인
3. 두 곳 모두 토큰이 없으면 [이슈](https://github.com/hmyanghm/claude-usage-menubar/issues)에 알려주세요.

---

## 동작 원리

OAuth 토큰(macOS: Keychain, Windows: `~/.claude/.credentials.json`)을 읽어 Anthropic Usage API를 호출합니다. 토큰/비용 상세는 `~/.claude/`의 로컬 JSONL 세션 파일에서 계산합니다. API 장애 시 로컬 추정치로 폴백합니다. OAuth 토큰이 만료되면 refresh token으로 자동 갱신합니다.
