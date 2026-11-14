# 금융 데이터 수집기

2025년 1월 2일 기준 코스피100 종목을 대상으로 KRX 일별 시세와 OpenDART 연간 재무제표를 수집합니다. 기본 시세 기간은 2021~2025년입니다.

`collect_design.py`가 API 요청, 법인코드 조회와 수집을 수행합니다. `schema.sql`은 PostgreSQL 테이블 초안이며 `load_postgres.py`로 수동 적재합니다. 자동 수집과 적재의 연결은 아직 없습니다.

Python 3.12 이상이 필요하며 추가 패키지는 없습니다. KRX 공식 구성종목 CSV와 거래일 CSV를 준비하고 `KRX_API_KEY`, `DART_API_KEY` 환경변수를 설정하세요. 인증키는 저장소에 올리지 않습니다.

```powershell
python collect_design.py prices --universe 종목목록.csv --calendar 거래일목록.csv
python collect_design.py financials --universe 종목목록.csv
python -m unittest -v
```

거래일 CSV는 `date` 열과 `YYYY-MM-DD` 날짜를 포함해야 합니다. 가격 수집과 재무 수집은 개별 실행할 수 있습니다. 결과는 `data/final-design/`에 저장됩니다. 기존 수집 결과 재사용과 `--refresh` 재조회가 가능합니다.

OpenDART 실 API는 일부 기업만 검증했습니다. DB 적재는 실제 PostgreSQL에서 검증되지 않았습니다. 재무제표는 최신 정정본일 수 있으므로, 과거 시점에 공개된 자료만 사용하는 백테스트에는 공시 시각과 이전 보고서 버전 확인이 필요합니다.

## 자동 수집

`auto_collect.py`를 매일 한 번 실행하면 전날까지 거래일 가격을 확인하고 누락을 재시도합니다. 새 사업보고서와 정정 공시도 조회해 해당 기업의 재무제표를 수집합니다. 처음에는 최근 7일 공시를 확인하며, 이후에는 마지막 확인일부터 다시 조회합니다. 과거 재무제표 전체 수집은 별도 초기 작업입니다.

```powershell
python auto_collect.py --universe 종목목록.csv --calendar 거래일목록.csv --env-file 설정파일경로
```

이 명령을 실행할 장비의 작업 스케줄러나 서버의 배치 작업에 등록해야 매일 실행됩니다. 자동 등록은 아직 하지 않았습니다. 장비가 켜져 있어야 하며 KRX 자료가 제공된 이후 시각으로 예약하세요. 거래일 목록은 계속 갱신해야 합니다. 오류·대기 내역은 `data/final-design/automation/`에 기록되고 실패 시 종료코드 1을 반환합니다. API 장애나 미제공 시 다음 실행에서 재시도합니다.

OpenDART 재무제표 API는 최신 보고서를 반환할 수 있습니다. 공시 접수번호와 다르면 다른 버전으로 잘못 간주하지 않고 대기 상태로 남깁니다. 정확한 공시 시각 확인과 자동 DB 적재는 아직 구현하지 않았습니다.

## 과거 최초·정정 보고서 원본 수집

`filing_versions.py`는 공시 목록에서 최초본과 정정본을 찾아 접수번호별 XBRL ZIP과 접수일·다음 거래일·해시를 저장합니다. `--start`와 `--end`는 사업연도가 아닌 **공시 접수일**입니다. 최초 학습기간보다 이른 보고서부터 수집해야 하며, 누락이나 미제공 원본은 보고서에 기록됩니다.

```powershell
python filing_versions.py --universe 종목목록.csv --calendar 거래일목록.csv --env-file 설정파일경로 --start 2019-01-01 --end 2025-12-31
```

수집한 원본은 `filings/종목코드/접수번호/`에 저장됩니다. `select_filing()`은 기준일에 사용 가능한 최신 사업연도·공시 버전을 찾습니다. 자동 수집기도 새 공시별 XBRL을 보관합니다.

`extract_xbrl.py`는 표준 IFRS 계정 6개(자산·자본·지배기업 자본·매출·순이익·지배기업 순이익)를 연결 또는 별도 기준으로 추출합니다. 계정이 누락되거나 값이 충돌하면 추정하지 않습니다. 삼성전자 2024년 사업보고서의 6개 수치를 OpenDART 재무제표 API와 대조했습니다. 다른 기업의 정정 보고서 두 건도 추출했으나 정정 전 최초보고서와의 수치 대조는 아직 못 했습니다. 비표준 계정과 다양한 XBRL 형식은 검증이 더 필요합니다. 통화 KRW, 연결·별도 축이 명시된 연간 XBRL만 처리합니다.

```powershell
python extract_xbrl.py 원본.zip --period-end 2024-12-31 --scope CFS --out 추출결과.json
```

정확한 공시 시각 자동 확인, 전체 계정 추출과 백테스트 연동은 아직 구현하지 않았습니다. 모든 보고서의 원본 제공 여부도 확인해야 합니다. 거래일 목록에는 공시일 이후 거래일이 필요합니다.

## PostgreSQL 수동 적재

`load_postgres.py`는 `schema.sql`의 금융 테이블에 종목, 정리된 일봉 CSV와 접수번호별 XBRL에서 확인된 계정을 적재합니다. Python 표준 라이브러리 외에 PostgreSQL 드라이버가 필요합니다.

```powershell
python -m pip install "psycopg[binary]>=3.1,<4"
$env:DATABASE_URL = 'postgresql://사용자:비밀번호@서버:5432/DB이름'
# 미리 준비한 DB에 schema.sql 적용 후, 이 폴더에서 실행
python load_postgres.py stocks --universe 종목목록.csv
python load_postgres.py prices --universe 종목목록.csv --prices-dir data/final-design/prices
python load_postgres.py financial --universe 종목목록.csv --metadata 보고서폴더/metadata.json --published-at 2025-03-25T16:00:00+09:00
```

위 시각은 형식 예시이며 실제 공시 시각이 아닙니다. 재무보고서는 공시일을 시간으로 임의 변환하지 않고, 출처에서 확인한 공개 시각이 있을 때만 적재합니다. 원본 ZIP과 메타데이터는 같은 폴더에 있어야 하며 해시를 검증합니다. 재무 수치가 충돌하면 적재를 중단합니다. 누락된 표준 계정은 저장하지 않고 표시합니다. 동일한 보고서 및 계정은 중복 삽입하지 않습니다. 주가 적재는 한 번의 실행 전체를 트랜잭션으로 처리하며 오류 시 전부 취소됩니다.

미래 시점 유출을 막으려면 전체 공시 이력의 누락·정정 관계, 지배주주 수치의 의미와 공식 거래일을 검증하고 백테스트에서 적합한 버전을 선택해야 합니다. 해당 연결과 `supersedes_id` 자동 연결은 아직 구현하지 않았습니다. 로컬 파일 경로는 해당 장비에서만 유효합니다. 팀 서버 배포 전 공유 스토리지 경로를 정해야 합니다.
