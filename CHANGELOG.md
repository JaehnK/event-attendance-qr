# Changelog

버전은 [SemVer](https://semver.org/lang/ko/)를 따르고, 릴리스마다 `main`에 `vX.Y.Z` 태그를 답니다.

## [0.2.0]

### 변경
- 관리자 로그인을 42 OAuth에서 Google OAuth로 바꿨습니다. `ADMIN_EMAILS`에 있는 계정만 `/qr`에 접근할 수 있습니다(`ADMIN_LOGINS` 대체).
- 회차별 출석 행을 미리 만들어 두고, 스캔하면 상태만 채웁니다. 관리자가 미리 입력한 공가·사전 결석은 그대로 유지됩니다.

### 추가
- clasp로 Apps Script 배포를 자동화했습니다(`scripts/deploy-gas.sh`). 기존 배포를 갱신하므로 `/exec` URL이 유지됩니다.
- Apps Script 하네스 테스트(`tests/apps_script_harness.js`)를 추가했습니다.

### 수정
- 결석 처리(`markAbsent`)가 Google 쪽 일시 오류를 만나면 최대 3회 재시도합니다. 결석 행은 묶어서 기록합니다.

## [0.1.0]

- 42 인트라 로그인 기반 회차별 QR 출석 초기 버전입니다.
