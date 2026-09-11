# 로컬 실행과 운영 인수인계

이번 구성은 개인 연구용 로컬 실행이다. 회사 prod-01에는 접속하거나 배포하지 않았다.
앱, PostgreSQL, 데이터 수집 도구는 실제 주문 기능을 제공하지 않는다.

## 실행 형태

| 구성 요소 | 로컬 주소 | 저장 위치 |
| --- | --- | --- |
| SvelteKit + FastAPI 웹 작업실 | 127.0.0.1:8765 | 호스트의 var/accounts·var/research |
| PostgreSQL 18.6 | 127.0.0.1:55432 | trading-research의 postgres-data 볼륨 |
| Python 직접 실행 Streamlit | 127.0.0.1:8501 | 계좌·판단은 호스트 파일, 기존 분석은 위 DB |
| Docker Streamlit | 127.0.0.1:8502 | 연구 DB, 호스트 개인 파일 공유 없음 |

새 웹 작업실은 `mise run web-build` 후 `mise run web`으로 실행한다.
저장 자료 조회에 DB·토스 인증은 필요하지 않으며 정적 파일과 API를 같은 출처에서 제공한다.
설치·개발·합성 자료 검증은 [웹 작업실 안내](WEB_WORKBENCH.md)를 따른다.
현재 Docker 이미지는 기존 Streamlit용이며 새 웹앱을 포함한 이미지로 전환하지 않았다.

기존 Streamlit의 분석 화면은 같은 DB를 보는 대안이다. Docker 화면은 UID 10001, 읽기 전용 파일 시스템,
128MiB 임시 공간, CPU 2개·메모리 2GiB 제한으로 실행한다. HTTP 포트는 호스트의
localhost에만 연결한다. 앱 컨테이너 내부의 `0.0.0.0`은 이 포트 연결에 필요하다.
로컬 웹 화면에 별도 로그인 기능은 없다.

Python 3.14.7과 uv 0.12.10의 Docker 기반 이미지를 digest로 고정했고,
의존성은 `uv.lock`에서 설치한다. 실행 이미지에는 개발 도구와 Git 이력·`.env`·`var/`를
넣지 않는다. `.dockerignore`의 허용 목록에는 코드와 합성 예시 설정만 포함된다.

## Docker만으로 처음 실행하기

저장소 루트에서 아래 명령을 순서대로 실행한다. 기존 DB에 동일한 합성 자료를
가져오는 요청은 변경 없이 종료한다. 다른 내용으로 기존 자료 ID를 덮어쓰지는 않는다.

```bash
docker compose up -d --wait postgres
docker compose -f compose.yaml -f compose.app.yaml build app
docker compose -f compose.yaml -f compose.app.yaml run --rm app trading doctor
docker compose -f compose.yaml -f compose.app.yaml run --rm app trading db-upgrade
docker compose -f compose.yaml -f compose.app.yaml run --rm app python scripts/seed_demo.py
docker compose -f compose.yaml -f compose.app.yaml up -d --wait app
```

화면은 <http://127.0.0.1:8502>다. 합성 자료 생성 단계는 시연이 필요한 경우에만 사용한다.
새 버전 코드를 적용할 때는 이미지를 다시 빌드하고, 변경된 마이그레이션을 적용한 다음
`up -d --wait app`을 실행한다. 시작할 때 자동으로 스키마를 바꾸지는 않는다.
Python 직접 실행 중 코드를 수정했다면 화면 새로고침만 하지 말고 프로세스를 다시 시작한다.
계산 중에는 소스 파일을 수정하지 않는다. 실행 코드와 결과에 기록하는 소스 해시를 같은
버전으로 유지하려면 연구 기록 생성에 읽기 전용 컨테이너 이미지를 사용하는 편이 확실하다.

```bash
docker compose -f compose.yaml -f compose.app.yaml exec -T app trading doctor
docker compose -f compose.yaml -f compose.app.yaml exec -T app alembic check
docker compose -f compose.yaml -f compose.app.yaml ps
docker compose -f compose.yaml -f compose.app.yaml logs --tail 80 app
```

HTTP healthcheck는 웹 프로세스 응답만 검사한다. `doctor`는 DB 연결을,
`alembic check`는 모델과 DB 스키마의 일치를 별도로 검사한다. 세 검사는 투자 자료의
품질이나 수익성을 보장하지 않는다. 로그는 컨테이너별 10MB 파일 3개로 제한된다.

## 환경 설정

Python은 `.env`를 자동으로 읽지 않는다. Compose는 `.env`를 치환에 사용한다.
`TRADING_DATABASE_URL`이 명시되면 그 URL을 우선 사용한다. 그 외에는
`TRADING_DATABASE_HOST`, `PORT`, `NAME`, `USER`, `PASSWORD`를 모두 설정하거나,
아무것도 설정하지 않고 로컬 개발 기본값을 사용한다. 각 이름에는
`TRADING_DATABASE_` 접두사를 붙인다. 관련 없는 `DATABASE_URL`은 읽지 않는다.

컨테이너 앱은 `postgres:5432`와 Compose의 비밀번호를 자동으로 사용한다.
Mac에서 직접 실행하는 앱의 비밀번호·포트를 바꿀 때는 동일한 값을 환경 변수로
설정해야 한다. 비밀번호에 `@`, `/` 등의 문자가 있다면 구성 요소 방식이 URL을
직접 조합하는 것보다 편리하다. 설정 오류나 연결 확인 결과에 비밀번호를 출력하지 않는다.

기존 PostgreSQL 볼륨의 비밀번호는 Compose 환경 변수만 바꾼다고 변경되지 않는다.
현재 기본값 `local-research-only`는 이 로컬 개발 구성 전용이다.

## 종료와 복구

```bash
docker compose -f compose.yaml -f compose.app.yaml stop
```

볼륨을 유지하므로 다음 시작에 자료가 남는다. `down -v`와 볼륨 삭제는 일반 종료나
복구 절차에 사용하지 않는다. [로컬 백업 검사](LOCAL_BACKUP.md)는 원본 연구 DB의
일관된 snapshot을 덤프하고, 새 임시 DB로 복원한 뒤 행 단위 해시를 비교한다.
덤프는 `var/backups/`에 보존하며 별도 보관 장치로의 전송이나 정기 백업 예약은 설정하지 않았다.
계좌·AI 연구·시장 수집 파일은 DB 밖에 있으므로 [개인 자료 백업](ARTIFACT_BACKUP.md)을
별도로 실행한다. 이 복원 검사도 기존 저장소를 덮어쓰지 않고 새 디렉터리에서 수행한다.

## 실전 자료와 서버 배포에 남은 작업

기존 분석 도구의 기본 자료는 합성 자료다. 토스 실제 인증·시장·계좌 조회의 검증 이력은
[인수인계](HANDOFF.md)를 따른다. 저장한 원응답을 실제 추천에 사용하려면 공급자의 과거 종목군,
공개 시각, 총수익 조정, 기업행사, 거래 가능 시각과 비용을 검증한 입력이 필요하다.
현재 실행 결과는 그 공백을 메우지 않는다.

prod-01의 문서상 자원과 실제 가용 자원은 다르다. 배포를 시작할 때 현재 부하를 다시
확인하고 연구 전용 DB·계정·네트워크·볼륨을 분리해야 한다. 외부 접근이 필요하면 인증과
TLS를 구성한다. 현 로컬 기본 비밀번호나 인증 없는 화면을 외부에 공개하지 않는다.

구성 근거는 [uv Docker 가이드](https://docs.astral.sh/uv/guides/integration/docker/),
[Docker 빌드 지침](https://docs.docker.com/build/building/best-practices/),
[PostgreSQL 덤프 문서](https://www.postgresql.org/docs/18/backup-dump.html)를 확인했다.

## 이번 실행의 확인 범위

2026-09-10 로컬 Docker ARM64에서 UID·파일 시스템·자원 제한·포트 바인딩을 실제 검사했다.
화면에서 추천을 저장했고 DB 연결, 스키마 일치, 합성 자료 멱등 가져오기를 확인했다.
Mac과 Linux ARM64의 추천·백테스트·아홉 조건 평가 JSON이 전부 같았고,
로컬 에뮬레이션의 Linux AMD64에서도 아홉 조건 평가 JSON이 같았다.
AMD64 이미지를 빌드·실행했다는 사실은 회사 서버에서 배포·부하 검사를 마쳤다는 뜻은 아니다.
