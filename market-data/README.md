# 금융 데이터 수집기

2025년 1월 2일 기준 코스피100 종목을 대상으로 KRX 일별 시세와 OpenDART 연간 재무제표를 수집합니다. 기본 시세 기간은 2021~2025년입니다.

`collect_design.py`가 API 요청, 법인코드 조회와 수집을 수행합니다. `schema.sql`은 PostgreSQL 테이블 초안이며 DB에 자동 적재하지 않습니다.

Python 3.12 이상이 필요하며 추가 패키지는 없습니다. KRX 공식 구성종목 CSV와 거래일 CSV를 준비하고 `KRX_API_KEY`, `DART_API_KEY` 환경변수를 설정하세요. 인증키는 저장소에 올리지 않습니다.

```powershell
python collect_design.py prices --universe 종목목록.csv --calendar 거래일목록.csv
python collect_design.py financials --universe 종목목록.csv
python -m unittest -v
```

거래일 CSV는 `date` 열과 `YYYY-MM-DD` 날짜를 포함해야 합니다. 가격 수집과 재무 수집은 개별 실행할 수 있습니다. 결과는 `data/final-design/`에 저장됩니다. 기존 수집 결과 재사용과 `--refresh` 재조회가 가능합니다.

현재 실 API 호출 및 DB 적재는 검증되지 않았습니다. 재무제표는 최신 정정본일 수 있으므로, 과거 시점에 공개된 자료만 사용하는 백테스트에는 공시 시각과 이전 보고서 버전 확인이 필요합니다.

## 자동 수집

`auto_collect.py`를 매일 한 번 실행하면 전날까지 거래일 가격을 확인하고 누락을 재시도합니다. 새 사업보고서와 정정 공시도 조회해 해당 기업의 재무제표를 수집합니다. 처음에는 최근 7일 공시를 확인하며, 이후에는 마지막 확인일부터 다시 조회합니다. 과거 재무제표 전체 수집은 별도 초기 작업입니다.

```powershell
python auto_collect.py --universe 종목목록.csv --calendar 거래일목록.csv --env-file 설정파일경로
```

이 명령을 실행할 장비의 작업 스케줄러나 서버의 배치 작업에 등록해야 매일 실행됩니다. 자동 등록은 아직 하지 않았습니다. 장비가 켜져 있어야 하며 KRX 자료가 제공된 이후 시각으로 예약하세요. 거래일 목록은 계속 갱신해야 합니다. 오류·대기 내역은 `data/final-design/automation/`에 기록되고 실패 시 종료코드 1을 반환합니다. API 장애나 미제공 시 다음 실행에서 재시도합니다.

OpenDART 재무제표 API는 최신 보고서를 반환할 수 있습니다. 공시 접수번호와 다르면 다른 버전으로 잘못 간주하지 않고 대기 상태로 남깁니다. 과거 버전의 XBRL 수집, 정확한 공시 시각과 다음 거래일 연결, DB 적재는 아직 구현하지 않았습니다.
