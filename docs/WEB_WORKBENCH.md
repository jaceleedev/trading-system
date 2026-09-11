# 웹 투자 작업실

Svelte 5·SvelteKit·TypeScript 화면과 FastAPI가 저장된 계좌 관측과 AI 연구 기록을
연결한다. 기본 주소는 <http://127.0.0.1:8765>다. 사용자가 계좌 관측을 선택한 뒤
판단에서 원래 가설·찬반 근거·재검토 기록을 따라 읽을 수 있다.

18번의 [작업 실행](JOBS.md)은 API의 `--jobs` 옵션으로 활성화한다. 저장 자료 검증을
제출하고 예약·진행 상태·취소를 확인할 수 있다. 기존 계좌·연구 조회는 계속 DB 없이 가능하다.

## 설치와 실행

저장소 루트에서 실행한다. Python 3.14.7, Node 24.18.0, pnpm 11.13.0은
`.mise.toml`로 고정하며 Python과 프론트 의존성은 각각의 잠금 파일을 사용한다.

```bash
mise trust
mise install
mise run web-setup
mise run web-build
mise run web
```

FastAPI가 `web/build` 정적 파일과 API를 같은 출처에서 제공한다. Node는 개발·빌드에만
필요하다. 첫 빌드 전에는 API만 사용할 수 있으며 화면 요청은 404다.
종료는 실행한 터미널에서 Ctrl-C를 누른다. 계좌·연구 파일과 DB 볼륨은 삭제하지 않는다.

웹 작업실 조회는 `var/accounts`와 `var/research` 파일을 사용하며 PostgreSQL·토스 API·
키체인을 호출하지 않는다. 개인 자료는 정적 빌드나 Git에 포함하지 않는다.
기존 Streamlit 추천·백테스트는 `mise run ui`와 [기존 사용법](USER_GUIDE.md)을 따른다.

개발 중에는 두 터미널에서 각각 실행한다.

```bash
mise run web
```

```bash
mise run web-dev
```

개발 화면은 <http://127.0.0.1:5173>이고 `/api` 요청을 8765 API로 프록시한다.
Python 소스를 바꾸면 API 프로세스를 다시 시작한다. 개발 서버가 지원하는 출처만
프록시에서 변환하며 외부 사이트의 Origin을 신뢰된 값으로 바꾸지 않는다.

## 화면의 의미

- 계좌는 기본 선택하지 않는다. 관측을 선택하면 통화별 매수 가능 금액과 보유·진행 중
  주문을 표시한다. 소수 수량·가격·금액은 정확한 소수 문자열을 유지한다.
- 실제 현금 잔고는 미확인이다. KRW와 USD를 합산해 총자산을 만들지 않는다.
  조회 범위 밖 주문이나 다른 자산까지 확인했다는 의미도 아니다.
- 수집 완료 시각과 오래된 관측 여부를 표시한다. ‘저장 자료 다시 읽기’는 파일 재조회다.
  새 토스 관측 수집은 [MCP](CODEX_TOOLS.md) 또는 [계좌 CLI](TOSS_ACCOUNT.md)를 사용한다.
- 판단과 근거의 종류·모드·본문을 읽고 연결된 기록으로 이동한다. 계좌 선택을 바꾸어도
  과거 판단이 참조한 원래 계좌 관측은 바뀌지 않는다.
- 문맥에 포함된 기록만 목록과 검색 대상으로 삼는다. 생략 수와 누락된 참조를 표시하며,
  연결된 기록은 ID로 별도 조회할 수 있다. 검색 결과를 전체 연구 저장소의 결과로 해석하지 않는다.
- 근거·가설·판단·리뷰는 앞으로의 판단, 사후 분석, 합성 예시를 구분한다. 모델명은 작성자
  선언이며 실제 모델 실행 증명이 아니다. 저장된 제안은 주문·체결이 아니다.
- 재검토 목록은 저장 기록에서 계산한다. 사건 감시·예약 실행·AI 재기동 기능은 후속 범위다.
- 자료 읽기나 검증이 실패하면 오류를 표시한다. 이전 응답을 새 관측 성공으로 표시하지 않는다.

## API와 계약

| GET 경로 | 반환하는 내용 |
| --- | --- |
| `/api/v1/health` | 웹 API 상태와 읽기 전용·합성 표시 설정 |
| `/api/v1/account-snapshots` | 저장된 계좌 관측 선택 정보 |
| `/api/v1/context?snapshot_id=<ID>&max_records=50` | 기존 Python 서비스가 구성한 계좌·연구·재검토 문맥 |
| `/api/v1/research/<ID>` | 검증한 연구 기록과 원래 ID |
| `/api/v1/openapi.json` | 공개 응답 타입과 오류 계약 |

`snapshot_id`는 생략 가능하며 `max_records`는 1~100이다. 계좌번호·토큰·원응답을
HTTP로 노출하지 않는다. 오류는 `{ "error": { "code": "...", "message": "..." } }`로
응답하며 내부 예외나 검증 실패 입력을 반환하지 않는다.

API 모델은 소수 문자열·미확인 값·원래 시각·선택적 필드의 생략을 보존한다.
계좌 순번 `account_seq`는 원본 서비스의 정수를 HTTP에서 십진 문자열로 변환해
브라우저의 큰 정수 반올림을 방지한다. 계좌 선택의 키는 스냅샷 ID다.
OpenAPI와 Hey API 생성 클라이언트를 함께 버전 관리한다. API를 수정한 뒤에는 다음으로
재생성하고 변경 내용을 검토한다.

```bash
uv run python scripts/export_openapi.py
pnpm --dir web api:generate
```

웹·MCP·CLI는 기존 Python 조회·검증을 재사용한다. HTTP에서 MCP 프로토콜을 호출하거나
별도 금융 계산을 구현하지 않는다. 18번의 worker는 저장 자료 검증·읽기 전용 수집을
수행하며 [작업 안내](JOBS.md)의 실행 옵션을 따른다. 별도 모델 서비스는 도입하지 않았다.

20번의 [AI 조사 패널](INVESTIGATIONS.md)은 목적·선택 입력으로 조사와 재검토를 접수한다.
명시적으로 허용한 별도 worker에서 기존 Codex CLI를 실행하며 현재 버전·이전 완료 결과·
후속 자료 수집·선언된 출처와 실행 관측을 구분한다. 실제 주문은 실행하지 않는다.

21번의 [자금 계획 패널](CAPITAL_PLANS.md)은 판단·조사에 수량·가격·비용 가정을 연결해
대안을 계산한다. `--jobs`를 사용하면 계획 저장과 선택한 대안의 로컬 자금·보유 배정을
사용할 수 있다. 기존 배정·미확인 금액·예상 매도 대금을 구분하며 증권사 주문은 전송하지 않는다.

19번의 [시장 관측 패널](MARKET_OBSERVATIONS.md)은 응답 계약을 고정한 저장 캡처를
종목·통화·분봉/일봉·수정주가·기준 시각으로 선택한다. 정확한 가격·거래량은 십진 문자열로
보존하고 캔버스에만 숫자로 변환한다. 사건은 검증된 근거 상세에 연결한다. 누락 기간을
채우거나 관측을 최종 봉·실제 체결로 표시하지 않는다. 이 조회에도 DB·토스 인증은 필요 없다.

## 로컬 접근 경계

기본 서버는 loopback에만 바인딩한다. 다른 호스트·브라우저 출처의 요청과 교차 사이트
요청을 거부하며, 응답에 `no-store`를 설정한다. 정적 파일은 별도 빌드 폴더에서만 제공한다.
이는 같은 Mac 사용자에게 제공하는 로컬 앱이다. 원격 공개·다중 사용자 인증은 구현 범위가 아니다.

## 합성 자료로 검증하기

아래 경로는 반드시 존재하지 않는 새 디렉터리여야 한다. 생성기는 프로젝트의 실제
`var/` 하위와 기존 파일·디렉터리를 거부한다. 예시 계좌 2개와 서로 연결된 합성 연구
기록 5개를 만들며 토스·키체인·DB에 접속하지 않는다.

```bash
uv run python scripts/seed_web_demo.py --root /tmp/trading-web-demo
uv run trading-web --workspace /tmp/trading-web-demo --static-dir "$PWD/web/build" --synthetic --port 8766
```

<http://127.0.0.1:8766>에서 합성 예시를 확인한다. `--synthetic`은 실행자가 지정한
표시 설정이며 내용의 진실성을 인증하지 않는다. 일반 개인 작업실 실행에는 사용하지 않는다.

## 개발 검증

```bash
TRADING_TEST_DB=1 uv run pytest
uv run ruff check .
uv run ruff format --check .
mise run web-check
pnpm --dir web format:check
mise run web-build
pnpm --dir web exec playwright install chromium
pnpm --dir web test:e2e
```

PostgreSQL 통합 테스트는 `127.0.0.1:55432/trading`만 사용한다. 브라우저 테스트는
8871·8872 포트에서 임시 합성·빈 작업 공간과 실제 빌드된 프론트·FastAPI를 실행하고 종료한다.
기존 서버나 개인 자료를 테스트 대상으로 재사용하지 않는다. 브라우저 산출물은 시스템 임시
디렉터리에 저장한다. Python 검사나 모의 화면 검증을 실제 투자 수익성 증거로 해석하지 않는다.

관련 구현 근거: [SvelteKit 정적 어댑터](https://svelte.dev/docs/kit/adapter-static),
[FastAPI 클라이언트 생성](https://fastapi.tiangolo.com/advanced/generate-clients/),
[TanStack Svelte Query](https://tanstack.com/query/latest/docs/framework/svelte/overview).
