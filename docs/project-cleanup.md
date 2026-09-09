# 프로젝트 정리 기록

정리일: 2026-09-09

사용자 요청에 따라 프로젝트를 문서 전용으로 전환했다. 이 폴더는 정리 당시 Git 저장소가 아니었다. 개발 코드, 테스트, 실행 설정, 데이터베이스, 시세 데이터, 생성 보고서와 캐시를 프로젝트 밖의 휴지통으로 옮겼다.

- 복구 위치: `/Users/jace/.Trash/trading-system-code-20260909-221117-413568`
- 이동한 파일: 36개
- 이동한 최상위 항목: 7개
- 이동 전후 모든 파일의 SHA-256 일치 확인
- 기존 README와 검증 기록은 원문을 변경하지 않고 docs/archive에 보존
- 기존 테스트와 과거 투자성과 수치는 이번 작업에서 재검증하지 않았다.

## 제거한 최상위 항목

- `.gitignore`
- `pyproject.toml`
- `configs`
- `data`
- `reports`
- `tests`
- `trading_system`

## 보존한 과거 문서

- [프로토타입 README](archive/prototype-readme.md)
- [프로토타입 검증 기록](archive/prototype-validation.md)

위 두 문서는 제거된 구현에 관한 역사적 기록이다. 그 안의 실행 명령과 파일 경로는 현재 프로젝트에서 유효하지 않다. 과거의 프로그램 테스트나 가상자산 예제를 현재 주식 전략의 검증 결과로 사용하지 않는다.

## 이동 파일 검증

| 원래 상대 경로 | SHA-256 |
|---|---|
| `.gitignore` | `b89fdcedbb57921683b06bb43be524948c375f582d5cad13c9b4307c8d3c9971` |
| `configs/research.toml` | `f5a99bcf9718d31d10781f71778b8c98b0fc1ec5846348619119dec82cab3450` |
| `data/demo.csv` | `12780e076fda66e9849f46678191495beaba2eac2f3708d62bec62627a979905` |
| `data/demo.csv.meta.json` | `54f25ed03333593e9d132e77bf29f90c4aeada5614594e2c01edd13592452508` |
| `data/krw-btc.csv` | `17386f294a1eb7298b4076f01455bcb76bb83e75af37d353c4d7b0182f5da200` |
| `data/krw-btc.csv.meta.json` | `4398cc8612f017a719ba5a8974479b806fa58c2e5f33797294b43dc58d9b5765` |
| `data/smoke-paper.sqlite` | `e800ca4fb5495a4b0099332f69c9f602c707fa0e0391fc9b013ee5efdd0ccc63` |
| `pyproject.toml` | `9761c28993cde5551381e337e1dea3d605a33e6ec957d422d7597b29ed62fc53` |
| `reports/demo/index.html` | `28dcecb65cc481894df4f092e3f365ce1495f6f069f67ff2678abf1333aa101b` |
| `reports/demo/result.json` | `4ccc9d8544319fc5ed7544d73f5ccb231b2e60cd7a8c3c6b51022fa86935dbfd` |
| `reports/krw-btc/index.html` | `5d93ab7ff99625676973656137798144d792a47b3cbeec201300cd8d5c63a9dd` |
| `reports/krw-btc/result.json` | `b5f34a730839c5efefbc34a934eb476d48d20ae1a5ce6cd8108fc320583433f2` |
| `tests/__init__.py` | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| `tests/__pycache__/__init__.cpython-314.pyc` | `5c903d9fafd3d501e6c6429082c645bee2a54fd35770d3db95ad2ce8031b6656` |
| `tests/__pycache__/test_cli.cpython-314.pyc` | `4044ef20b87f50f2af77ddaa8041f83e5ec661e6bc802215d7a8b80f61c71d4b` |
| `tests/__pycache__/test_core.cpython-314.pyc` | `27bd14377919071f3fc9d0599ff00336376d87c19dc7dfe7627b54699ba2ee3a` |
| `tests/__pycache__/test_data.cpython-314.pyc` | `78f4496f9824b2b5faf1a3fac80c2b0d712de92363d2750039c7536cff3ce2b1` |
| `tests/__pycache__/test_paper.cpython-314.pyc` | `2c17d37e1f0f1af573d3ea6c24ffc0da49cceb38b16d8129cc7562481a625496` |
| `tests/test_cli.py` | `d23ef5fab373f066c22250f6659abc89e890deaa58ef01e7937ef6a77b09dce4` |
| `tests/test_core.py` | `86a02ed1cb568d7617401aaabe4c4ae01cf1bcdef3102c32354d016381c2d8f2` |
| `tests/test_data.py` | `38a01084c7ce5f9194a60cd0c2b9f8c31906f0d1ba2bce51713c2c17596aeb80` |
| `tests/test_paper.py` | `2f9afcbc6da5237042560ac16929046d799bf8dcc0aa19253db08a846aa67c6f` |
| `trading_system/__init__.py` | `af96d45923f8e6c39987114cd578a1070f65af8471168a226c141a7617d0a1fd` |
| `trading_system/__main__.py` | `935a1c1166b0c1ea35a82256345000bf2c73ded718d77773bc27a71ecce28f7d` |
| `trading_system/__pycache__/__init__.cpython-314.pyc` | `995b2a20d7c68c3d421d80f80840d2cd225b52cf55f74efcf62862b7b2bc721f` |
| `trading_system/__pycache__/__main__.cpython-314.pyc` | `37119524fd02a72cbab3355eb4e8d7a273aec1ece527bc54b58f186785c4c1a2` |
| `trading_system/__pycache__/cli.cpython-314.pyc` | `25601b641b4bb517edc5820dc6eb0cd8e90d31a4616fc9961d608bb0ffb3f115` |
| `trading_system/__pycache__/core.cpython-314.pyc` | `ac0a512b18c1dad12d5b77cb00e89acef7a75d96fb96a8cff4759dd505b85c98` |
| `trading_system/__pycache__/data.cpython-314.pyc` | `f2481a8973325cbf6b7741c02ff9f58588010a8ac1f488e3df6459e09467b49a` |
| `trading_system/__pycache__/paper.cpython-314.pyc` | `c18f57f049cb7ae108518fa28c17f60fa586ca1047ec83466c7b674942379689` |
| `trading_system/__pycache__/report.cpython-314.pyc` | `28ad96600126c9fb44c2e9299ef4889dfafcef068f7a2de556ca27af0e0f2487` |
| `trading_system/cli.py` | `8a547257455e8e8046a3432f6b4069d6545d1789c044ab53e6f737408b4b9ad5` |
| `trading_system/core.py` | `fd03c16c9218e97b6f12b8f6becfd085c1ee5663b3874face3eb33dbd0f9d434` |
| `trading_system/data.py` | `cabc6a87eb43001ccb55d14b923c4b81f2f0c13c5615295d6b843245002baaaa` |
| `trading_system/paper.py` | `0dca45aae0bf120f2bbf10bc52607b342adceeab53acf12e630865eecc190f62` |
| `trading_system/report.py` | `0e3637d25924c1a2618ba99b124c8565508ea3995f6934a3df7f99f374abb9d6` |
