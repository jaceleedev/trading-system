# 로컬 백업과 복원 확인

프로젝트 루트에서 다음 명령을 실행한다.

```sh
.venv/bin/python scripts/verify_local_backup.py
```

대상은 `127.0.0.1:55432/trading`, 사용자 `trading`, Docker Compose 프로젝트 `trading-research`의 `postgres` 서비스뿐이다. 현재 Docker context의 Unix 소켓, 컨테이너 라벨·이미지·loopback 포트 바인딩과 PostgreSQL system identifier를 확인한다. 다른 호스트·포트·DB·URL query, 원격 Docker endpoint는 거부한다. 일반 `DATABASE_URL`은 사용하지 않으며, 앱 전용 `TRADING_DATABASE_URL`도 이 범위를 벗어나면 거부한다. `DOCKER_HOST` 또는 `PG*` 환경 변수가 있으면 연결 경로가 바뀌지 않도록 실행을 거부한다.

`var/backups`에 고유한 custom-format `.dump`와 검증 `.json`을 만든다. 새 디렉터리는 0700, 파일은 0600이며 기존 파일을 덮어쓰지 않는다. 출력 파일과 직접 부모 디렉터리의 심볼릭 링크는 거부한다. 비밀번호는 프로세스 인자·출력·검증 기록에 넣지 않는다. 컨테이너 안의 PostgreSQL 도구는 명시적인 로컬 Unix 소켓으로만 접속한다.

원본에서 read-only repeatable-read 트랜잭션을 열고 `pg_export_snapshot()`으로 얻은 스냅샷을 `pg_dump --snapshot`에 전달한다. 원본의 비교 값도 같은 트랜잭션에서 구하므로, 동시에 다른 작업이 데이터를 추가해도 서로 다른 시점 때문에 실패하지 않는다. PostgreSQL 공식 문서는 dump의 내부 일관성과 [custom-format 백업 및 복원](https://www.postgresql.org/docs/18/backup-dump.html), 다른 세션과 같은 스냅샷을 쓰는 [pg_dump --snapshot](https://www.postgresql.org/docs/18/app-pgdump.html)을 설명한다.

복원은 이번 실행이 새로 만든 `trading_restore_check_<UUID>` 데이터베이스에만 수행한다. `pg_restore --exit-on-error --single-transaction --no-owner --no-privileges`로 복원한 뒤, 모든 앱 테이블과 `alembic_version`의 행 수·전체 행 SHA-256, 컬럼·제약·인덱스 정의의 SHA-256, Alembic revision을 비교한다. 행은 UTC 등 고정된 세션 설정에서 PostgreSQL JSONB 텍스트로 표현하고 C collation으로 정렬한 뒤, 바이트 길이를 앞에 붙여 해시한다. 원본 행 내용이나 SQL 오류 원문은 검증 기록에 남기지 않는다.

임시 데이터베이스는 CREATE가 성공했고 그 OID와 이번 실행의 소유 표식이 그대로일 때만 삭제한다. 이름 충돌로 기존 데이터베이스를 발견하면 삭제하지 않는다. 소유 정보가 바뀌거나 정리에 실패하면 성공으로 기록하지 않는다. 원본에 DML·DROP·복원을 수행하지 않으며, 백업과 검증 기록은 보존한다. 실행이 강제로 종료되면 불완전한 dump·기록이나 임시 DB가 남을 수 있으므로 `status: passed`, `snapshot_comparison_passed: true`, `cleanup: dropped_owned_database`를 함께 확인한다.

이 검증은 현재 로컬 DB 자료의 논리 백업과 같은 서버 안에서의 복원을 확인한다. 별도 장치의 사본, 역할·tablespace, 운영체제·Docker volume 전체 복구, 다른 PostgreSQL 버전으로의 복원을 증명하지 않는다. DDL 충돌·권한 변경·디스크 부족 등은 실패할 수 있다. 이 스크립트는 예약 실행이나 운영 DB용 백업 서비스가 아니다.
