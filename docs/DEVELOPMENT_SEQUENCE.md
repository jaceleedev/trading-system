# 2026-09-14 승인된 웹 후속 개발 순서

사용자는 아래 세 기능을 기능별 브랜치와 별도의 새 Codex 작업으로 순서대로
구현·검증·로컬 커밋하도록 승인했다. 각 작업은 직전 기능의 완료 커밋에서 시작한다.
조정 작업이 다음 작업을 만들며 구현 작업이 직접 다음 작업이나 자동화를 만들지 않는다.
원본 `/Users/jace/Desktop/trading-system` 체크아웃과 기존 브랜치 끝점을 보존한다.

| 순서 | 브랜치 | 범위와 경계 |
| --- | --- | --- |
| 27 | `feat/27-workbench-operations` | API 활성화, DB 접근, 같은 workspace의 worker 생존과 Codex·네트워크 허용 설정을 분리한다. 독립 worker 등록/heartbeat, 확인 가능한 대기 이유, 계좌·시장 자료 시각과 신선도, 재검토 판단·실행/대기 조사 요약, 필요한 동안의 제한적 자동 재조회를 제공한다. |
| 28 | `feat/28-web-observation-capture` | 명시적인 계좌·시장 관측 수집 접수와 완료 결과 연결을 제공한다. 기존 `account-sync`/`market-capture`, credential resolver, `--allow-network` 경계를 사용한다. 계좌·endpoint·종목·조회 및 페이지 한도를 명시적으로 선택하며 완료 ID·시각·수집 범위를 표시한다. 같은 요청의 재확인은 원래 request key와 입력을 유지한다. |
| 29 | `feat/29-guided-investment-flow` | 조사→계획·대안→모의 원장→기간 결과의 선택 유지와 다음 단계 이동을 제공한다. 조사 revision/출력→계획/대안→원장/보고서의 존재와 연결을 서버에서 재검증한다. 새로고침·뒤로 가기·계좌 전환에도 안전하게 이어진다. 단계 이동은 조회/입력 채우기이며 배정·실행·계산을 자동 발생시키지 않는다. |

27번 시작점은 `main`의 기능 26 병합 커밋
`6c3eddb5d05cb1ec3585b2da83aa4a5cb159ea01`이다. 각 기능의 실제 완료 상태,
검증 결과, migration과 이어갈 범위는 [HANDOFF.md](HANDOFF.md) 상단에서 확인한다.
27번은 `feat/27-workbench-operations`에서 구현·검증을 완료했다. Python 2,189개,
Vitest 38개, 웹 E2E 86개 및 필수 정적 검사·빌드가 통과했다. 완료 SHA는
`b04ca337544df94b6a71259dd52bc7a023ea8af9`다.

28번은 이 커밋에서 `feat/28-web-observation-capture`로 이어 구현·검증을 완료했다.
Python 2,227개, Vitest 45개, 웹 E2E 103개 및 필수 검사·빌드, 격리된 실제
API·DB·worker·데스크톱/모바일 브라우저 검증이 통과했다. 새 migration은 없다.
실제 외부 수집은 합성 transport로 격리했으며 새 주문·모델 호출은 하지 않았다.
정확한 로컬 완료 SHA는 브랜치 끝점과 `/tmp/trading-feature28-completion.txt`를 따른다.
29번은 아직 구현하지 않았다. 조정 작업이 28번 완료 커밋에서 별도 새 작업을 만든다.

## 새 작업의 구현 원칙

- `AGENTS.md`, `docs/HANDOFF.md`, `docs/DEVELOPMENT_ROADMAP.md`,
  `docs/WEB_WORKBENCH.md`와 Git 상태·로그를 먼저 확인한다.
- 한 작업에는 한 기능만 포함한다. API/worker·웹·독립 검토를 병렬로 나눌 때
  파일 소유 범위를 구분하고 마지막에는 통합 동작을 검증한다.
- DB 없이 저장 자료를 읽는 경로, 명시적인 계좌 선택, 정확한 소수 문자열,
  미확인 숫자, 관측·수집·기록 시각의 구분을 보존한다.
- worker의 허용 설정은 로그인, 외부 API 성공이나 투자 실행 준비의 증명이 아니다.
  작업 시도 heartbeat를 worker 생존 관측으로 사용하지 않는다. 여러 worker의
  유효 시각과 허용 기능을 각각 확인한다.
- 원격 push·PR 생성·병합, 회사 서버·DB, 유료 서비스 추가, 상시 서비스 설치,
  실제 주문은 승인 범위에 없다. 주문 전송은 계속 비활성이다.

## 완료 게이트

개발 중 집중 검사를 수행하고 기능 완료 시 다음을 모두 실행한다.

```sh
TRADING_TEST_DB=1 uv run pytest
uv run ruff check .
uv run ruff format --check .
mise run web-check
pnpm --dir web format:check
mise run web-build
pnpm --dir web test:e2e
git diff --check
```

합성 자료는 새 격리 workspace에만 쓴다. 통합 DB와 migration 대상은
`127.0.0.1:55432/trading`뿐이며 다른 작업의 자료를 지우거나 전체 DB를 초기화하지 않는다.
실제 FastAPI·DB·worker와 빌드된 앱을 브라우저에서 검증한다. 27번에서는 idle/running/stale
worker, DB 장애, 기능이 다른 복수 worker와 재시작을 확인한다. 합성 fixture 검증과
실제 외부 관측은 명확히 구분한다. 연결된 trading MCP의 private 자료를 검증 fixture로
사용하지 않는다. 자격증명은 출력·인수·문서·Git에 넣지 않는다.

모든 필수 검사 통과와 인계 갱신 이후 로컬 커밋한다. 실제 실패·제약, 완료 브랜치,
검사 수치, migration 유무, 다음 범위를 남기고 최종 응답에 정확한 commit SHA를 보고한다.
완료 전에 다음 기능으로 넘어가지 않는다.

## 세 기능 이후 별도 검토

출처 저장 강화, 새 가격·공시를 반영한 재판단 조건 개선, 실제 자료를 이용한
모의운용은 27~29번 뒤 별도 후속 검토 항목이다. 이 문서의 승인으로 해당 작업이나
실제 투자 실행이 자동 승인되지는 않는다.
