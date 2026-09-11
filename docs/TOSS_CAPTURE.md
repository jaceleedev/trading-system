# 토스증권 공개 시장 자료 수집

확인 기준은 2026-09-10의 공식 OpenAPI **1.2.15**다. 공개
[OpenAPI 명세](https://openapi.tossinvest.com/openapi-docs/latest/openapi.json)와
[개요](https://openapi.tossinvest.com/openapi-docs/overview.md)를 확인했다.
기능 19는 별도로 고정한 응답 계약이 있는 분봉·일봉을 관측 뷰와 차트로 읽는다.
최종 투자·백테스트 자료로 자동 변환하지 않는다. [관측과 시점 계약](MARKET_OBSERVATIONS.md)을 따른다.

```bash
# 인증이나 네트워크 호출 없이 고정한 계약 확인
uv run trading provider-info

# 사용자가 별도로 발급한 TOSS_ACCESS_TOKEN이 실행 환경에 있을 때만 실제 조회
uv run trading capture-market --endpoint candles \
  --query configs/toss-candles.example.json --pages 3 --output var/captures

# 저장된 수집 파일의 무결성 확인
uv run trading inspect-capture var/captures/내용해시.json
```

토큰을 명령행 인수·Git 파일·문서·채팅에 넣지 않는다. 토큰 발급/캐시는 별도의
[인증 도구](TOSS_AUTH.md)가 담당하며 `capture-market --authenticate`로 연결한다.
허용 IP 등록은 토스 WTS에서 직접 수행한다. 공식 명세는 Client Credentials Bearer 인증과 허용 IP를
요구하며, 새 토큰을 발급하면 기존 토큰이 폐기될 수 있다. 이번 구현 작업에서는 토큰을
발급하거나 실제 인증된 API 요청을 하지 않았다.

## 구현한 범위

허용 경로는 GET 여섯 개뿐이다. 계좌·보유·주문·조건 주문 경로는 선택할 수 없다.

| CLI 이름 | GET 경로 | 목적 |
|---|---|---|
| candles | /api/v1/candles | 일봉·분봉 원응답 |
| stocks | /api/v1/stocks | 지정 종목 기본 정보 |
| stock-list | /api/v1/stocks/all | 거래소별 제공 종목 목록 |
| fx | /api/v1/exchange-rate | 표시 환율과 기준 환율 |
| calendar-kr | /api/v1/market-calendar/KR | 국내 세션 정보 |
| calendar-us | /api/v1/market-calendar/US | 미국 세션 정보 |

요청 파라미터는 배포본에 포함된 계약으로 검사하고, Bearer는 헤더로만 전달한다.
리다이렉트는 거부하며, 오류 본문과 요청 헤더를 출력·저장하지 않는다. 응답에 토큰이
그대로 반사되면 저장을 거부한다. 응답 크기는 10 MiB, 각 호출 시간 제한은 15초다.
직접 수집기는 한 프로세스에서 요청 시작 간격이 최소 1.1초다. 여러 직접 수집기의
한도를 합산하지 않는다. 기능 18의 worker들은 같은 로컬 DB의 공통 gate를 사용하지만
직접 CLI/MCP 호출까지 통합하지는 않는다. 429는 자동 재시도하지 않고 종료한다.

수집 파일은 `provider`, `endpoint`, `query`, `retrieved_at`, `response`,
`contract_sha256`을 포함한다. 새 캔들 수집에는 별도 `response_contract_sha256`도 넣는다.
`retrieved_at`은 **우리 시스템이 응답을 받은 시각**이다.
과거 봉이 원래 공개됐던 시각으로 바꾸지 않는다. 내용 해시 파일명으로 변경 없이 보존하고
새 파일에는 0600, 새 수집 디렉터리에는 0700 권한을 사용한다. 기존 파일을 덮어쓰지 않는다.

일봉은 페이지당 최대 200개, 한 명령에서 최대 50페이지다. `nextBefore`를 그대로 넘긴다.
명세상 `before`는 inclusive이므로 경계 봉이 중복될 수 있다. 원응답을 그대로 남기며
중복 봉을 서로 다른 거래일로 계산하지 않는다. 커서가 반복되거나 과거로 진행하지 않으면
종료한다. 중간 실패 전까지 저장한 페이지는 조사할 수 있도록 보존한다. `--pages`만큼
종료한 것은 전체 과거 이력의 완전한 수집을 뜻하지 않는다.

## 추천·백테스트 자료로 바로 사용할 수 없는 이유

- 최신 고정 응답 계약의 `timestamp`는 분봉의 **종료** 시각 또는 일봉의 현지 자정이다.
  이전 문서의 ‘봉 시작’ 설명을 수정했다. 응답에는 장 종료·최초 공개 시각·최종 확정 여부가 없다.
- `adjusted=true`의 설명은 ‘수정주가’다. 배당 재투자를 포함하는 총수익 자료라고 명시하지 않는다.
- 현재 제공 종목 목록은 당시 전체 상장 종목군이 아니다. 업종의 과거 이력과 정보 공개 시각도 없다.
- 기업행사의 권리일·지급일·비율·배당금 원장은 별도 확보가 필요하다.
- 캔들이 어느 국내 통합 세션·미국 연장 세션을 포함하는지, 달력의 어느 종료 시각과 맞는지
  자료 공급 기준을 확인해야 한다.
- 환율의 `rate`는 매수 표시 환율, `midRate`는 기준율이다. 실제 환전율·거래비용과 같다고
  가정하지 않는다. 사용 목적에 맞는 환율 정의와 시점이 필요하다.

따라서 capture 명령의 성공은 공급자 응답을 보존했다는 뜻이다. 필요한 근거를 보완한
공급자 어댑터가 [데이터 계약](DATA_CONTRACT.md)을 충족해야 추천·백테스트에 연결할 수 있다.
이 분리 덕분에 부족한 필드를 추측해 만든 결과를 투자 근거로 제시하지 않는다.
