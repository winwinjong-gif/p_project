# 금융 데이터 수집기

2025년 1월 2일 기준 코스피100 종목을 대상으로 KRX 일별 시세와 OpenDART 연간 재무제표를 수집합니다. 기본 시세 기간은 2021~2025년입니다.

`collect_design.py`가 수집을 실행하며, `collector.py`의 API 요청 및 OpenDART 법인코드 조회 기능을 사용합니다. `schema.sql`은 PostgreSQL 테이블 초안이며 DB에 자동 적재하지 않습니다.

Python 3.12 이상이 필요하며 추가 패키지는 없습니다. KRX 공식 구성종목 CSV와 거래일 CSV를 준비하고 `KRX_API_KEY`, `DART_API_KEY` 환경변수를 설정하세요. 인증키는 저장소에 올리지 않습니다.

```powershell
python collect_design.py prices --universe 종목목록.csv --calendar 거래일목록.csv
python collect_design.py financials --universe 종목목록.csv
python -m unittest -v
```

거래일 CSV는 `date` 열과 `YYYY-MM-DD` 날짜를 포함해야 합니다. 가격 수집과 재무 수집은 개별 실행할 수 있습니다. 결과는 `data/final-design/`에 저장됩니다. 기존 수집 결과 재사용과 `--refresh` 재조회가 가능합니다.

현재 실 API 호출 및 DB 적재는 검증되지 않았습니다. 재무제표는 최신 정정본일 수 있으므로, 과거 시점에 공개된 자료만 사용하는 백테스트에는 공시 시각과 이전 보고서 버전 확인이 필요합니다.
