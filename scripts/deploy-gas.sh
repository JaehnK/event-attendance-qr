#!/usr/bin/env bash
# apps-script.gs 를 테스트한 뒤 Apps Script 에 올리고, 기존 웹 앱 배포를 새 버전으로 갱신 (URL 유지).
# 사전: npm i -g @google/clasp && clasp login (jaehn@gsia.kr), Apps Script API 사용 설정.
set -euo pipefail
cd "$(dirname "$0")/.."

# /usr/bin/clasp(Potassco ASP 솔버)와 이름이 겹치므로 npm 전역의 Google clasp 를 직접 지정
CLASP="${CLASP:-$(npm prefix -g)/bin/clasp}"
"$CLASP" --version >/dev/null

node tests/apps_script_harness.js apps-script.gs

# clasp 는 rootDir(gas/) 만 올린다: 코드 1개 + 매니페스트
mkdir -p gas
cp apps-script.gs gas/apps-script.js
cp appsscript.json gas/appsscript.json

deployment=$(python3 -c 'import json;print(json.load(open(".clasp.json"))["deploymentId"])')
"$CLASP" push --force
"$CLASP" deploy --deploymentId "$deployment" --description "${1:-$(git log -1 --format=%h\ %s)}"
echo "배포 갱신 완료: $deployment"
