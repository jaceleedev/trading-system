# 토스 자격증명과 인증

사용자는 2026-09-10 토스 Open API의 Client ID와 Client Secret을 이미 발급받았다고
확인했다. 값 자체는 대화나 저장소에 제공하지 않았다. 키 발급 완료와 실제 연결 검증은
구분한다. 이 기능은 로컬 인증과 시장 조회를 지원하며 주문을 전송하지 않는다.

## Mac에서 한 번 설정하기

프로젝트 디렉터리의 **본인 로컬 터미널**에서 실행한다.

```sh
uv run trading toss-auth configure
uv run trading toss-auth status
```

Client ID와 Secret은 화면에 표시되지 않는 입력란에 직접 입력한다. 파이프·리다이렉트와
일반 텍스트 입력으로의 우회는 거부한다. macOS Keychain의 이 프로젝트 전용 항목에
저장하며 코드·Git·환경 파일·명령행 인수에 기록하지 않는다. OS가 키체인 접근 허용을
요청할 수 있다. Linux의 임의 keyring 백엔드나 평문 파일로 자동 전환하지 않는다.

`status`는 설정 유무만 반환하고 인증 요청을 하지 않는다. 환경 변수의 우선순위와
누락 상태를 확인할 수 있지만 Secret이나 Client ID, 토큰을 출력하지 않는다.

토스 WTS의 **설정 → Open API → 허용 IP 관리**에 실제 실행 위치의 공인 IP를 등록한다.
그 다음 토큰을 확인한다.

```sh
uv run trading toss-auth check
```

이 명령은 유효한 로컬 토큰이 있으면 재사용하고, 필요할 때 공식 인증 서버에 요청한다.
`token_available`은 토큰을 확보했다는 의미다. 환경에서 제공한 토큰은 유효성을 별도로
확인하지 않으며, 이 결과만으로 계좌 조회나 시세 연결에 성공했다고 해석하지 않는다.

## 토큰 사용

```sh
uv run trading capture-market --authenticate --endpoint candles \
  --query configs/toss-candles.example.json --pages 1
```

기존 `capture-market` 동작은 유지한다. `--authenticate`를 주지 않으면
`TOSS_ACCESS_TOKEN`만 읽고 Client ID·Secret으로 토큰을 발급하지 않는다.

인증 도구는 명시적인 `TOSS_ACCESS_TOKEN`을 가장 먼저 사용한다. 그다음
`TOSS_CLIENT_ID`와 `TOSS_CLIENT_SECRET`을 모두 설정한 환경 또는 macOS 키체인의
자격증명을 사용한다. 두 환경 변수 중 하나만 있으면 키체인으로 조용히 넘어가지 않는다.
환경 변수를 사용해도 새 토큰의 영속 캐시는 Mac 키체인을 요구한다. 컨테이너에는
외부에서 발급·관리한 `TOSS_ACCESS_TOKEN`을 전달하는 방식만 지원한다.

토스는 client당 유효 토큰이 하나이며 재발급 시 이전 토큰을 즉시 무효화한다.
이 구현은 Client ID별 캐시와 같은 Mac 사용자 내 프로세스 잠금을 사용한다. Secret이
바뀌면 `configure`로 다시 등록해 이전 캐시를 제거한다. 다른 Mac이나
컨테이너, 다른 프로그램까지 잠금을 공유하지 않으므로 **토큰 발급 주체는 한 곳으로 둔다**.
토큰 만료 전에 여유 시간을 두고 재발급하며, 응답 오류·시간 초과에 자동 재시도하지 않는다.
네트워크 오류가 났어도 서버가 발급했을 가능성이 있으므로 오류를 감추고 반복 요청하지 않는다.

리다이렉트와 환경 프록시를 사용하지 않으며 요청 대상은 공식 HTTPS 인증 주소로 고정한다.
오류 본문과 원본 예외, 응답 헤더, 인증 값은 로그나 CLI 출력에 남기지 않는다.
키체인이 잠겼거나 저장에 실패하면 평문 저장으로 대신하지 않고 종료한다.

## 확인 범위

모의 HTTP 전송과 모의 키체인으로 정상·만료·오류·비밀값 비노출·잠금 동작을 검사한다.
실제 인증 검증은 사용자가 로컬 설정을 마친 뒤 별도로 수행한다. 키 발급 사실만으로
허용 IP·계좌 권한·시세 이용 조건까지 확인됐다고 표시하지 않는다.

공식 계약: 2026-09-10 확인한 OpenAPI v1.2.15의
[`POST /oauth2/token`](https://openapi.tossinvest.com/openapi-docs/latest/openapi.json).
자격증명 저장 구현은 [Python keyring의 macOS 백엔드](https://keyring.readthedocs.io/en/latest/index.html)를 사용한다.
