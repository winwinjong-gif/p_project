"""수집한 종목, 일봉, 재무제표를 PostgreSQL에 적재한다."""
import argparse  # 적재 종류와 파일 경로를 입력받는다.
import calendar  # 사업연도 마지막 날짜를 계산한다.
import csv  # 수집한 일봉 CSV를 읽는다.
import datetime as dt  # 거래일과 공시 시각을 확인한다.
import hashlib  # 재무제표 원본이 바뀌지 않았는지 확인한다.
import json  # 재무제표 메타데이터를 읽는다.
import os  # DB 접속 정보를 환경변수로 읽는다.
from pathlib import Path  # 파일과 폴더 경로를 다룬다.
import re  # 보고서명과 코드의 형식을 확인한다.
import sys  # 실행 결과를 운영체제에 전달한다.
import uuid  # 재실행 시 같은 재무 항목에 같은 ID를 쓴다.
from zoneinfo import ZoneInfo  # 공시 시각의 한국 날짜를 확인한다.

from collect_design import AS_OF, snapshot
from extract_xbrl import ACCOUNTS, extract

REPORT_NAMESPACE = uuid.UUID('6ee17c7c-6aa4-4ed6-8185-40a488748fa9')


# 기준일의 고정 종목을 등록한다. 같은 코드는 이름과 기준일을 갱신한다.
def load_stocks(connection, universe):
    with connection.cursor() as cursor:
        for code, name in universe.items():
            cursor.execute('''INSERT INTO mr_stocks (code, name, universe_as_of)
                VALUES (%s, %s, %s)
                ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name,
                universe_as_of = EXCLUDED.universe_as_of,
                updated_at = CURRENT_TIMESTAMP AT TIME ZONE 'UTC' ''', (code, name, AS_OF))
    return len(universe)


# 수집 파일을 한 행씩 확인한 뒤 같은 종목과 날짜에 중복 없이 저장한다.
def load_prices(connection, paths, universe):
    count = 0
    seen = set()
    with connection.cursor() as cursor:
        for path in paths:
            with path.open(encoding='utf-8-sig', newline='') as file:
                for row in csv.DictReader(file):
                    code = row['code']
                    day = dt.date.fromisoformat(row['date'])
                    if code not in universe or (code, day) in seen:
                        raise ValueError('대상 외 종목 또는 중복 종목·날짜')
                    seen.add((code, day))
                    op, high, low, close, volume = (int(row[field]) for field in
                                                     ('open', 'high', 'low', 'close', 'volume'))
                    if min(op, high, low, close, volume) < 0:
                        raise ValueError('가격 또는 거래량이 음수입니다')
                    if min(op, high, low, close) == 0 and volume != 0:
                        raise ValueError('가격이 0인데 거래량이 있습니다')
                    if min(op, high, low, close) > 0 and not low <= min(op, close) <= max(op, close) <= high:
                        raise ValueError('시가·고가·저가·종가 관계가 잘못됐습니다')
                    collected = dt.datetime.fromisoformat(row['collected_at'])
                    if collected.tzinfo is None or collected.utcoffset() is None:
                        raise ValueError('수집 시각에는 시간대가 필요합니다')
                    collected = collected.astimezone(dt.timezone.utc).replace(tzinfo=None)
                    cursor.execute('''INSERT INTO mr_daily_prices
                        (code, date, open, high, low, close, volume, source, price_basis, collected_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (code, date) DO UPDATE SET open=EXCLUDED.open,
                        high=EXCLUDED.high, low=EXCLUDED.low, close=EXCLUDED.close,
                        volume=EXCLUDED.volume, source=EXCLUDED.source,
                        price_basis=EXCLUDED.price_basis, collected_at=EXCLUDED.collected_at''',
                        (code, day, op, high, low, close, volume, row['source'], row['price_basis'], collected))
                    count += 1
    return count


# 보고서 원본과 접수 정보를 확인해 정정 전후를 서로 다른 보고서로 저장한다.
def load_financial(connection, metadata_path, published_at, scope, universe):
    meta = json.loads(metadata_path.read_text(encoding='utf-8'))
    code, receipt = meta['code'], meta['receipt_no']
    if code not in universe or not re.fullmatch(r'\d{14}', receipt):
        raise ValueError('대상 외 종목 또는 잘못된 접수번호')
    if published_at.tzinfo is None or published_at.utcoffset() is None:
        raise ValueError('공개 시각에 시간대가 필요합니다')
    if published_at.astimezone(ZoneInfo('Asia/Seoul')).date().isoformat() != meta['published_date']:
        raise ValueError('공개 시각의 한국 날짜가 접수일과 다릅니다')
    published_utc = published_at.astimezone(dt.timezone.utc).replace(tzinfo=None)
    available = dt.date.fromisoformat(meta['available_date'])
    if available <= published_at.astimezone(ZoneInfo('Asia/Seoul')).date():
        raise ValueError('사용가능일은 공시일 이후여야 합니다')
    match = re.search(r'\((\d{4})\.(\d{2})\)', meta['report_name'])
    if not match or int(match[1]) != int(meta['business_year']):
        raise ValueError('보고서 사업연도 오류')
    year, month = int(match[1]), int(match[2])
    period_end = dt.date(year, month, calendar.monthrange(year, month)[1])
    archive = metadata_path.parent / 'statement.zip'
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != meta['sha256']:
        raise ValueError('재무제표 원본 해시 불일치')
    accounts = extract(archive, period_end, scope)
    if any(item['status'] == 'conflict' for item in accounts.values()):
        raise ValueError('수치가 충돌하는 계정이 있어 적재하지 않습니다')
    items = [(name, item) for name, item in accounts.items() if item['status'] == 'ok']
    if not items:
        raise ValueError('확인 가능한 재무 계정이 없습니다')
    report_id = str(uuid.uuid5(REPORT_NAMESPACE, f'{receipt}:{scope}'))
    with connection.cursor() as cursor:
        cursor.execute('''INSERT INTO mr_financial_reports
            (id, code, receipt_no, business_year, scope, published_at, available_date,
             source_file, source_sha256) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (receipt_no, scope) DO NOTHING RETURNING id''',
            (report_id, code, receipt, year, scope, published_utc, available, str(archive), digest))
        inserted = cursor.fetchone()
        if not inserted:
            cursor.execute('''SELECT id, code, business_year, published_at, available_date, source_sha256
                FROM mr_financial_reports WHERE receipt_no=%s AND scope=%s''', (receipt, scope))
            existing = cursor.fetchone()
            if not existing or tuple(existing[1:]) != (code, year, published_utc, available, digest):
                raise ValueError('기존 보고서 정보가 다릅니다. 확인 후 처리하세요')
            report_id = existing[0]
        for name, item in items:
            item_id = str(uuid.uuid5(REPORT_NAMESPACE, f'{report_id}:{name}:current'))
            statement = 'BS' if ACCOUNTS[name] == 'instant' else 'IS'
            cursor.execute('''INSERT INTO mr_financial_items
                (id, report_id, statement_type, account_id, account_name, account_detail,
                 period_name, amount, currency, unit_scale)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO NOTHING''',
                (item_id, report_id, statement, 'ifrs-full_' + name, name, '',
                 'current', item['amount'], item['unit'], 1))
    return len(items), [name for name, item in accounts.items() if item['status'] == 'missing']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind', choices=('stocks', 'prices', 'financial'))
    parser.add_argument('--universe', required=True, type=Path)
    parser.add_argument('--prices-dir', type=Path)
    parser.add_argument('--metadata', type=Path)
    parser.add_argument('--published-at', type=dt.datetime.fromisoformat,
                        help='공식 공시 공개 시각 (시간대 필수, 예: 2025-03-25T16:00:00+09:00)')
    parser.add_argument('--scope', choices=('CFS', 'OFS'), default='CFS')
    args = parser.parse_args()
    if args.kind == 'prices' and not args.prices_dir:
        parser.error('prices에는 --prices-dir이 필요합니다')
    if args.kind == 'financial' and (not args.metadata or not args.published_at):
        parser.error('financial에는 --metadata와 확인된 --published-at이 필요합니다')
    dsn = os.environ.get('DATABASE_URL')
    if not dsn:
        parser.error('DATABASE_URL 환경변수가 필요합니다')
    try:
        import psycopg  # PostgreSQL에 안전하게 연결하고 SQL을 실행한다.
    except ImportError:
        parser.error('psycopg 패키지가 필요합니다. README의 설치 안내를 확인하세요')
    universe, _ = snapshot(args.universe)
    paths = sorted(args.prices_dir.rglob('*.csv')) if args.prices_dir else []
    if args.kind == 'prices' and not paths:
        parser.error('일봉 CSV 파일이 없습니다')
    # 예외 발생 시 전체 변경을 취소하고 정상 종료 시에만 저장한다.
    with psycopg.connect(dsn) as connection:
        load_stocks(connection, universe)
        if args.kind == 'prices':
            print('적재한 일봉 행:', load_prices(connection, paths, universe))
        elif args.kind == 'financial':
            count, missing = load_financial(connection, args.metadata, args.published_at, args.scope, universe)
            print('적재한 재무 항목:', count, '미확인 항목:', ', '.join(missing) or '없음')
        else:
            print('적재한 종목:', len(universe))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, KeyError, OSError) as error:
        sys.exit(f'적재 실패: {error}')
