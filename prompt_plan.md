# Claude Usage Monitor - 구현 계획

## Phase 1: 코어 기능 (완료)
- [x] rumps 기반 메뉴바 앱 구조
- [x] macOS Keychain OAuth 토큰 조회
- [x] Anthropic Usage API 호출
- [x] JSONL 파일 파싱 (UsageTracker)
- [x] 세션(5h)/주간(7d) 프로그레스 바
- [x] 토큰/비용 상세 서브메뉴

## Phase 2: 안정성 개선 (완료)
- [x] API 429 에러 백오프 처리
- [x] API 응답 캐시 (TTL 10분)
- [x] API 장애 시 로컬 비용 기반 추정치 폴백
- [x] 계정 전환 감지 및 캐시 초기화
- [x] 메시지 ID 기반 중복 제거
- [x] Dock 아이콘 숨김

## Phase 3: UX 개선 (완료)
- [x] 메뉴바 표시 설정 (타이틀 모드, 섹션 토글)
- [x] 잠자기 복귀 시 자동 새로고침
- [x] 갱신 주기 정상화 (5분 간격)
- [x] 소넷 전용 주간 사용량 표시

## Phase 4: 배포 (완료)
- [x] setup.sh 설치 스크립트
- [x] build_dmg.sh 빌드 스크립트
- [x] PyInstaller .spec 설정
- [x] 코드서명 (ad-hoc)
- [x] DMG 패키징
- [x] v1.0.5 릴리스

## Phase 5: 향후 개선 (완료)
- [x] 사용량 임계치 알림 (80%/90% 도달 시 macOS 알림, 설정 토글 가능)
- [x] 사용량 히스토리 그래프 (7일 스파크라인 + 일별 상세 서브메뉴)
- [ ] ~~다크모드 대응 아이콘~~ (건너뜀)
- [ ] ~~universal binary (arm64 + x86_64) 빌드~~ (건너뜀 — 본인 전용)
- [x] 자동 업데이트 기능 (GitHub releases 기반, 1시간 주기 확인)
- [x] OAuth 토큰 자동 갱신 (refresh token 기반, 만료 시 자동 갱신)
