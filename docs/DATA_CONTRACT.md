# 데이터 계약 v1

폴더 하나에 `manifest.json`, `instruments.csv`, `bars.csv`, `fx.csv`를 둔다.
열 이름과 파일을 자동 추측하지 않는다. CSV는 UTF-8이며, 날짜는 ISO 8601,
시각은 UTC offset을 포함한다. 가격·환율은 양수 유한 십진수, 거래량은 0 이상이다.
최대 정밀도는 정수 18자리·소수 10자리다.

| 파일 | 필수 열 |
|---|---|
| instruments.csv | instrument_id, symbol, name, market, currency, sector, listed_on, delisted_on, known_at |
| bars.csv | instrument_id, session_date, session_close_at, available_at, open, high, low, close, adjusted_close, volume, is_final |
| fx.csv | currency, date, krw_per_unit, available_at |

instrument_id는 티커 재사용과 무관한 영구 식별자다. KR은 KRW, US는 USD를 사용한다.
delisted_on만 빈 값을 허용한다. known_at은 해당 종목 메타데이터를 알 수 있었던 시각이다.
업종·종목명 정정 등 자료 개정은 새로운 dataset_id로 보존한다. 단일 종목 메타데이터가
역사 전체에 적용되므로, 업종 변화를 포함한 복잡한 시점별 메타데이터는 현재 지원하지 않는다.

bars의 session_close_at은 **공급자가 명시한 해당 거래일의 종료 시각**이다.
해당 시장 현지 날짜가 session_date와 같고 available_at이 종료 이후인지 검사한다.
is_final은 문자열 `true`여야 한다. 검사 통과가 공급자의 거래소 달력·시각 주장의
정확성을 검증한 것은 아니다. 진행 중인 봉·종가 추정치는 입력하지 않는다.

raw OHLC는 주문·평가용, adjusted_close는 배당·분할 반영 모멘텀 신호용이다.
조정가격의 누적수익과 실제 계좌의 배당 현금을 중복 합산하지 않는다.
CSV 구조 통과만으로 기업행사 정확도·생존편향 제거·투자 가능성이 입증되지 않는다.
환율은 USD 1단위당 KRW이며, 누락 환율을 1로 채우지 않는다.

manifest의 필수 키:

```json
{
  "schema_version": 1,
  "dataset_id": "provider-revision-001",
  "label": "자료의 설명",
  "source": "공급자와 취득 경로 및 조정 방식의 근거",
  "kind": "historical",
  "universe": "unknown",
  "adjustment": "total_return"
}
```

kind는 synthetic/historical/observed, universe는 point_in_time/survivors_only/unknown이다.
point_in_time도 공급자의 선언이며 자체 감사 완료 표시가 아니다. 현재 생존 종목만
다운로드한 자료를 point_in_time이라고 표시해서는 안 된다.

전체 원문 파일의 SHA256과 manifest를 저장한다. 동일 데이터 재수입은 no-op이고,
같은 dataset_id에 다른 자료를 덮어쓰는 작업은 거부한다. 수입은 한 DB 트랜잭션이다.
메모리의 manifest는 중첩 목록까지 변경을 차단한다. 원문 해시와 별도로 정규화한
manifest·종목·가격·환율 전체의 내용 해시를 추천·백테스트 ID에 포함한다. 같은 값의
DB 소수 자릿수와 시간대 표현, 행 순서가 달라져도 내용 해시는 유지된다.

선택 키 `corporate_actions`는 분할·현금 배당 배열이다. 백테스트에서 원가격 평가와
배당을 함께 처리할 때 필요하며, 필드와 검증 방식은 [백테스트 계약](BACKTEST.md)을 따른다.

## 알려진 달력 제한

검토한 exchange_calendars 4.13.2의 XKRX 수능일 특수시간 목록은 2020년까지다.
따라서 이 라이브러리만으로 최근 한국 일봉의 확정 시각을 생성하지 않는다.
실제 자동 수집기를 붙이기 전에 공급자 시각과 KRX 특수일 공지를 대조해야 한다.
[확인한 소스](https://github.com/gerrymanoim/exchange_calendars/blob/4.13.2/exchange_calendars/xkrx_holidays.py#L1346)

demo-data는 실제 종목·실제 거래일 달력·실제 환율을 사용하지 않는 합성 fixture다.
기능 시연과 회귀 검사에만 사용한다. 데모 결과를 수익성 근거로 제시하지 않는다.
