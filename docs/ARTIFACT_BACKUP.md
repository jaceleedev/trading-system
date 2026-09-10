# 계좌·AI 판단·시장 수집 자료의 로컬 백업

`artifact-backup`은 로컬 `accounts`, `research`, `captures`, `investigations`, `capital-plans` 저장소의 완성된 객체를 복사하고 복원을 검증한다. 기존 [PostgreSQL 백업](LOCAL_BACKUP.md)과 별도 기능이다. 네트워크, 증권사 인증, 모델 호출, 주문, DB 연결은 수행하지 않는다.

목적지의 부모 디렉터리를 먼저 준비하고 매번 새 이름을 사용한다. `create` 목적지는 원본 자체·원본의 상위 디렉터리·실제 다섯 저장소 내부일 수 없다. `var` 전체를 재귀 조사하지 않으므로 `--source var`의 목적지를 `var/backups` 아래에 두는 것은 허용한다. `restore`의 백업과 목적지는 서로 포함 관계여서는 안 된다.

```sh
mkdir -p -m 700 var/backups var/recovery-checks
uv run trading artifact-backup create --source var --destination var/backups/artifacts-first
uv run trading artifact-backup verify --backup var/backups/artifacts-first
uv run trading artifact-backup restore --backup var/backups/artifacts-first --destination var/recovery-checks/artifacts-first
```

`create`는 복사된 객체와 참조 관계를 검증한다. `restore`는 기존 자료를 덮어쓰지 않고 새 디렉터리로 복사한 다음 원본 백업과 검증 결과를 비교한다. 출력에서 `status: passed`, `object_identity_checks_passed: true`, `reference_checks_passed: true`를 확인한다. 복원 확인에는 `restored_comparison_passed: true`도 필요하다. 출력은 건수·바이트 수·manifest SHA-256 등 메타데이터만 포함한다. 백업 manifest의 해시는 무결성 비교값이며 서명이나 공급자 진위 인증이 아니다.

새 백업은 v3 `manifest.json`과 다섯 저장소 디렉터리로 이루어진다. 기존 v1의 세 저장소 및 v2의 네 저장소 백업도 원래 manifest 바이트와 해시를 유지해 검증·복원한다. 압축파일을 풀거나 manifest가 지정한 임의 경로에 쓰지 않는다. 각 객체 파일명은 `<sha256>.json`이며 전체 canonical JSON 바이트의 SHA-256과 일치해야 한다. manifest도 canonical JSON으로 저장하며 복사와 참조 검증이 끝난 뒤 마지막으로 발행한다. 새 디렉터리는 0700, 파일은 0600이다. 실제 바이트를 새 파일에 쓰므로 원본·백업·복원본이 파일 inode를 공유하지 않는다.

원본 `--source` 아래의 다섯 저장소만 조사한다. Keychain, OAuth 토큰 캐시, `.env`, 소스 코드, DB, 기타 `var` 자료는 대상이 아니다. 저장소 안에서 객체 파일명과 맞지 않는 일반 파일은 내용 확인 없이 제외하고 건수만 기록한다. 임시 쓰기 파일도 여기에 해당한다. 저장소의 심볼릭 링크, FIFO, 장치, 중첩 디렉터리는 거부한다. 존재하지 않는 저장소는 `source_stores`에 `present: false`로 명시하며 복원본에도 만들지 않는다. 다섯 저장소가 모두 없으면 성공한 빈 백업이며, `object_count: 0`을 실제 자료 보존의 증거로 해석해서는 안 된다.

원본 상위 디렉터리와 목적지 부모는 현재 사용자 소유이고 다른 사용자가 쓸 수 없어야 한다. 저장소 디렉터리와 읽는 객체는 현재 사용자 소유이며 다른 사용자에게 접근 권한이 없어야 한다. 기존 공개 권한을 자동으로 변경하지 않는다. 선택된 경로의 심볼릭 링크와 직접 부모의 심볼릭 링크를 거부한다. 존재하는 목적지는 비어 있어도 거부한다.

복사한 계좌 객체는 원래 관측 schema·시각·요약을 검증한다. 시장 수집 자료는 허용된 시장 capture envelope, canonical bytes, 파일 identity를 검증한다. 이는 실제 투자용 시계열 데이터 검증과 다르다. AI 판단 기록은 복원본의 계좌 객체를 사용해 전체 참조 관계를 다시 확인한다. 누락·변조된 참조, 다른 종류의 객체, 잘못된 모드 연결을 건너뛰지 않는다. 원본 `recorded_at`, `retrieved_at`, 모델 선언, mode를 다시 생성하거나 수정하지 않는다. 오래된 계좌 관측의 신선도는 복원 후에도 실제 현재 시각을 기준으로 판단해야 한다.

조사 저장소는 고정 입력·모델 출력·실행 기록을 포함한다. 입력은 원래 계좌·연구·시장 자료와 대조하고 출력·이전 조사·실행의 참조를 검증한다. 큰 시장 응답의 발췌는 원본에서 동일하게 재계산한다. 저장된 실행 메타데이터의 형식과 참조를 확인하는 것이며 실제 모델 신원이나 DB의 최신 결과 채택을 인증하지 않는다. 작업·조사 revision의 PostgreSQL 이력은 별도 DB 백업이 필요하다.

자금 계획은 원래 계좌·판단/조사·시장 입력의 참조를 검증하고 예약 관측과 가정으로 계산을 재현한다. DB 등록 경쟁에서 남은 유효한 계획 파일도 포함한다. 계획 등록과 살아 있는 자금 배정은 DB 상태이며 이 파일 백업만으로 복원되지 않는다.

현재 제한은 객체 10,000개, 총 객체 크기 2 GiB, manifest 4 MiB이며 개별 계좌·판단 객체 16 MiB, 시장 capture 10 MiB이다. 자료 목록을 먼저 고정하므로 그 뒤 추가된 자료가 백업에서 빠질 수 있다. 포함한 판단의 참조가 완성되지 않으면 실패한다. 여러 저장소 전체를 같은 한 시점에서 얻은 atomic snapshot이라고 주장하지 않는다.

중단·디스크 부족·권한 변경이 발생하면 불완전한 새 디렉터리가 남을 수 있다. 완료된 manifest가 없거나 검증이 실패한 사본은 사용할 수 없다. 재시도에는 다른 새 목적지를 사용한다. 실패한 디렉터리와 기존 원본을 자동 삭제하지 않는다. 같은 장치 안의 이 검증은 별도 장치 보존, 클라우드 백업, 파일 암호화, PostgreSQL 복구를 증명하지 않는다.
