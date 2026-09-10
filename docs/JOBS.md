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
검증만 실행한다. 웹에서는 우선 저장 자료 검증 제출만 제공한다.

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
