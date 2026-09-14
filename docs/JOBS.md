# 작업 실행과 복구

PostgreSQL에 작업과 실행 시도를 보존하고 별도 Python worker가 실행한다.
작업실의 저장 자료 조회는 DB 없이도 가능하다. 작업 제출·조회·취소는 API를
`--jobs`로 실행했을 때 활성화된다. 증권사 주문은 실행하지 않는다.

## 시작하기

프로젝트 전용 PostgreSQL을 [운영 안내](OPERATIONS.md)에 따라 시작하고 저장소
루트에서 실행한다. 대상은 `127.0.0.1:55432/trading`이다.

```bash
uv sync --frozen
uv run trading db-upgrade
mise run web-build
mise run web-jobs
```

다른 터미널에서 worker를 시작한다.

```bash
mise run worker
```

웹의 작업 패널에서 저장 자료 검증을 제출한다. 계좌 관측은 제출 당시 ID로 고정한다.
예약 시각을 지정하면 그 시각 이후 실행 가능하고, 생략하면 바로 대기열에 넣는다.
worker의 대기열 확인 간격은 AI 재판단 주기나 포지션 보유 기간이 아니다.

## 운영 상태와 대기 이유 (기능 27)

`/api/v1/jobs/status`는 다음 관측을 구분한다. 응답 시각은 `checked_at`에 보존한다.

- `enabled`: 이 API 프로세스에서 작업 기능을 구성했는지 여부다.
- `database`: `not_checked`(작업 기능 비활성), `reachable`(조회 성공),
  `unavailable`(작업 저장소 접근 실패)이다. 장애 시 작업 수는 0이 아닌 `null`이다.
- `workers`: 같은 workspace의 독립 프로세스 세션별 시작·heartbeat·유효 기한·정지
  시각, idle/running/stopped 관측, live/stale/stopped 생존 판정과 허용 설정이다.
- `queued_count`/`running_count`: 같은 workspace의 DB 작업 수다. 제한된 최근 작업
  목록과 달리 전체 대기·실행 상태를 집계한다. 임대 만료 복구는 worker가 담당하며
  상태 GET은 작업을 인수·재시도·수정하지 않는다.

worker는 실행 시 새 세션을 등록하고, 작업 시도 heartbeat와 별도의 heartbeat를
idle 동안에도 갱신한다. 정상 종료는 stopped로 남기고 비정상 종료는 관측 유효 기한이
지나면 stale로 판정한다. 재시작은 새 세션이며 이전 세션의 시각·설정을 덮어쓰지 않는다.
업그레이드 이전 worker는 독립 등록이 없으므로 실행 시도가 보여도 생존을 확인할 수 없다.
API와 worker를 같은 코드로 재시작해야 한다.

대기 이유에는 예약 시각 전, 같은 workspace의 worker 없음, 실행 가능한 worker의
생존 관측 만료, 필요 기능 미허용, 실행 가능한 worker가 처리 중, 인수 대기가 있다.
여러 이유가 함께 나타날 수 있다. 각 worker의 유효 시각과 권한을 함께 평가하므로
살아 있는 비허용 worker와 만료된 허용 worker를 합쳐 실행 가능으로 표시하지 않는다.
worker와 대기 이유 목록은 최대 100개이며 생략 여부를 응답에 남긴다.

기본 `mise run worker`는 저장 자료 검증만 허용한다. Codex 조사에는
`--allow-codex`, 계좌·시장·브로커 GET 수집에는 `--allow-network`가 각각 필요하다.
두 설정은 독립적이다. Codex 웹 검색의 실제 runner 설정도
`codex_web_search_allowed`로 별도 표시하며 토스 수집 허용과 혼동하지 않는다.
관측되지 않은 이전 세션의 검색 설정은 `null`로 남긴다.
설정 관측은 실제 로그인, 모델 실행, 외부 API 성공이나 투자 실행
준비의 증명이 아니다. 상태 조회는 자격증명·모델·외부 API를 검사하지 않는다.

## CLI와 종류

```bash
uv run trading jobs submit --kind research-context --request-key first-context
uv run trading jobs list
uv run trading jobs show --id <작업-UUID>
uv run trading jobs cancel --id <작업-UUID>
uv run trading-worker --once
```

웹·CLI·worker의 `--workspace`는 같은 경로여야 한다. 작업은 해석된 절대 경로의 해시로
격리하므로 프로젝트를 옮기면 이전 작업을 자동 재개하지 않는다. 임의 셸 명령·코드·
URL·출력 경로를 작업 입력으로 받지 않는다. 입력은 `--parameters <JSON파일>`로 전달한다.

| 종류 | 입력 | 결과 |
| --- | --- | --- |
| `research-context` | 선택적 `snapshot_id`, `max_records`(1~100) | 저장 계좌·연구를 검증한 비공개 문맥 결과의 참조 |
| `account-sync` | 명시적 `account_seq`(양의 십진 문자열) | 기존 토스 클라이언트가 저장한 새 계좌 관측의 ID |
| `market-capture` | 기존 `endpoint` 별칭·`query`·`pages` | 허용된 시장 GET 원응답의 ID |
| `broker-sync` | 계좌·관측 모드·주문 생성일 범위·페이지 한도·선택적 상세 주문 IDs | [브로커 스캔](BROKER_RECONCILIATION.md) ID와 수집 완결성·중단 이유 |

기능 20의 내부 `investigation-run`은 [조사 서비스](INVESTIGATIONS.md)만 접수한다.
일반 작업 API·CLI에 임의로 제출할 수 없다. `--allow-codex` worker에서 Codex를 실행하고
현재 조사 버전과 실행 임대가 모두 유효할 때 결과를 채택한다.

계좌 순번은 계좌 목록에서 명시적으로 선택한다. 자격증명·토큰은 JSON이나 명령 인수에
넣지 않는다. 수집에는 기존 자격증명 해석기를 사용한다. 수집 작업을 실행하려면 worker에
`--allow-network`를 지정한다. 기본 worker는 수집 작업을 대기열에 남기고 저장 자료
검증만 실행한다. 웹에서는 저장 자료 검증과 [명시적인 계좌·시장 수집](OBSERVATION_CAPTURE.md)을 제공한다.
웹 계좌 수집은 선택 근거인 `source_snapshot_id`도 함께 고정한다. 기존 CLI의
명시적 `account_seq` 수집 계약은 유지한다.

같은 DB의 worker들은 토스 요청 경계에서 공통 잠금과 최소 호출 간격을 적용한다.
작업마다 새 클라이언트를 만들거나 작업 공간이 달라도 worker끼리의 제한은 유지한다.
기존 직접 MCP·CLI 호출과 다른 호스트의 호출까지 통합한 계정 전체 제한은 아니다.

## 접수·취소·재시도

같은 `request_key`와 입력은 기존 작업을 반환한다. 같은 키에 다른 입력·예약·시도 상한을
보내면 거부한다. 응답을 받지 못하면 원래 키와 입력으로 다시 확인한다. 새 키는 새 작업이다.

상태는 `queued`, `running`, `succeeded`, `failed`, `cancelled`다. 시도마다 소유자,
시작·heartbeat·종료 시각을 보존한다. 짧은 DB 트랜잭션에서 다른 worker가 잠근 작업을
건너뛰어 점유한다. 임대가 만료되면 남은 시도 범위에서 복구하고 이전 실행 토큰으로
완료를 기록하지 못하게 한다. 시도 상한은 작업별로 정하며 API·CLI에서는 1~5다.

대기 작업은 바로 취소한다. 실행 중에는 취소 요청을 기록하고 다음 확인 지점에서 멈춘다.
이미 시작한 외부 요청이나 저장을 되돌린다는 의미는 아니다. 장애로 수집이 반복될 수
있으며, 이 큐는 증권사 주문의 단 한 번 실행을 보장하지 않는다. 원본과 시도 이력을 함께 읽는다.

## 보존과 API

작업·시도 이력은 PostgreSQL 백업에 포함한다. 문맥 결과는 `var/jobs/results`의 비공개
파일에 보존한다. 이 조회 결과 파일은 기존 계좌·연구·시장 원응답 백업과 별도이며
원래 자료에서 다시 생성할 수 있다. 서버·worker 종료는 작업이나 원본 삭제가 아니다.

상태는 `/api/v1/jobs/status`, 목록·제출은 `/api/v1/jobs`, 상세는
`/api/v1/jobs/<UUID>`, 취소는 `/api/v1/jobs/<UUID>/cancel`이다. DB 장애와 입력 충돌을
구분하고 내부 예외·DB 접속 정보·실제 시도 토큰을 조회 응답에 포함하지 않는다.

실제 검증 결과는 [인수인계](HANDOFF.md)를 따른다.
참고: [PostgreSQL 행 잠금](https://www.postgresql.org/docs/current/sql-select.html),
[SQLAlchemy 트랜잭션](https://docs.sqlalchemy.org/en/20/core/connections.html).
