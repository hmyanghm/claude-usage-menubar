# Claude Usage Monitor - 기능 명세

## Feature 1: 사용량 대시보드 (메뉴바)
### 요구사항
1. 메뉴바 타이틀에 세션/주간/둘다 사용률 표시
2. 드롭다운에 세션(5h), 주간(7d), 소넷 전용 프로그레스 바 표시
3. 각 프로그레스 바에 리셋 시간 표시
4. API 장애 시 로컬 비용 기반 추정치로 폴백 (~ 접두사)

### 데이터 소스
- **Primary**: Anthropic OAuth Usage API (`/api/oauth/usage`)
- **Fallback**: 로컬 JSONL 파일 기반 비용 추정

## Feature 2: 세부 사용량 (서브메뉴)
### 요구사항
1. 오늘/세션(5h)/주간(7d) 토큰 및 비용 표시
2. 모델별 토큰/비용 분류
3. 캐시 토큰(생성/읽기) 별도 표시

## Feature 3: OAuth API 통신
### 요구사항
1. macOS Keychain에서 OAuth 토큰 조회
2. 계정 전환 감지 (토큰 prefix 비교) → 캐시 초기화
3. 429 에러 시 retry-after 또는 지수 백오프
4. API 응답 캐시 (TTL 10분)
5. 정상 호출 간격: 5분, 최대 백오프: 10분

### API 엔드포인트
- Usage: `GET /api/oauth/usage` (anthropic-beta: oauth-2025-04-20)
- Profile: `GET /api/oauth/profile`

## Feature 4: JSONL 파싱 (UsageTracker)
### 요구사항
1. ~/.claude/projects/**/*.jsonl + ~/.claude/history.jsonl 스캔
2. 메시지 ID 기반 중복 제거 (마지막 항목 우선)
3. stop_reason이 있는 최종 메시지만 카운트
4. 모델별 가격표 기반 비용 계산

### 지원 모델
- claude-sonnet-4-6, claude-opus-4-6, claude-sonnet-4-5 및 변형

## Feature 5: 표시 설정
### 요구사항
1. 타이틀 모드: 세션 / 주간 / 둘 다
2. 드롭다운 섹션 토글: 세션, 주간, 소넷
3. 설정 파일: `~/.claude/menubar_config.json`

## Feature 6: 시스템 통합
### 요구사항
1. 잠자기 복귀 시 5초 후 자동 새로고침 (force API call)
2. Dock 아이콘 숨김 (LSUIElement + NSApplicationActivationPolicyAccessory)
3. LaunchAgent로 로그인 시 자동 시작

## Feature 7: 배포
### 요구사항
1. setup.sh: venv 생성 + rumps 설치 + LaunchAgent 등록
2. build_dmg.sh: PyInstaller → .app → 코드서명 → .dmg
3. 타겟 아키텍처: arm64
