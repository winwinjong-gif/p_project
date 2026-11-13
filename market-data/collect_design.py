"""코스피100 종목의 일별 가격과 연간 재무제표를 수집한다."""
import argparse  # 실행할 때 수집 종류와 기간을 입력받는다.
import csv  # 종목 목록과 수집 결과를 CSV로 읽고 쓴다.
import datetime as dt  # 거래일과 수집 시각을 다룬다.
import hashlib  # 파일이 바뀌거나 손상됐는지 확인한다.
import io  # 파일로 저장하기 전에 CSV 내용을 만든다.
import json  # API 응답과 실행 결과를 읽고 쓴다.
import os  # 환경변수에서 API 키를 읽는다.
from pathlib import Path  # 파일과 폴더 경로를 다룬다.
import re  # 키와 종목코드의 형식을 확인한다.
import sys  # 실행 성공이나 실패를 운영체제에 알린다.

import collector as legacy  # 기존 코드의 API 요청과 법인코드 조회를 재사용한다.

# KRX 주소, 고정 종목 기준일, 정리된 가격 파일의 열 순서
KRX = 'https://data-dbg.krx.co.kr/svc/apis/sto/stk_bydd_trd'
AS_OF = '2025-01-02'
FIELDS = ('code', 'date', 'open', 'high', 'low', 'close', 'volume', 'source', 'price_basis', 'collected_at')


# 환경변수 또는 지정한 기존 .env 파일에서 필요한 키만 읽는다.
def api_key(name, env_file=None):
    value = os.environ.get(name, '').strip()
    if value or env_file is None:
        return value
    for line in env_file.read_text(encoding='utf-8-sig').splitlines():
        match = re.match(rf'^\s*(?:export\s+)?{re.escape(name)}\s*=\s*(.*?)\s*$', line)
        if match:
            value = match[1]
            if len(value) >= 2 and value[0] in ('\"', "'") and value[-1] == value[0]:
                return value[1:-1].strip()
            return re.split(r'\s+#', value, maxsplit=1)[0].strip()
    return ''


# 저장 도중 중단돼도 기존 파일이 깨지지 않도록 임시 파일을 먼저 쓴다.
def atomic_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_bytes(data)
    tmp.replace(path)


# 행 목록을 CSV 형식으로 변환한다.
def csv_bytes(rows, fields):
    buffer = io.StringIO(newline='')
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode('utf-8')


# 코스피100 목록을 읽고 100개 종목과 코드 중복을 확인한다.
def snapshot(path):
    raw = path.read_bytes()
    for encoding in ('utf-8-sig', 'cp949'):
        try:
            decoded = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError('Cannot decode constituent CSV')
    reader = csv.DictReader(io.StringIO(decoded))
    columns = set(reader.fieldnames or [])
    code_column = next((c for c in ('symbol', 'code', '종목코드', '단축코드') if c in columns), None)
    name_column = next((c for c in ('name', '종목명', '한글종목명') if c in columns), None)
    if not code_column or not name_column:
        raise ValueError('CSV must contain stock code and name columns')
    symbols = {}
    for row in reader:
        code, name = row[code_column].strip(), row[name_column].strip()
        if not re.fullmatch(r'[0-9A-Z]{6}', code) or not name or code in symbols:
            raise ValueError('Invalid or duplicate constituent; preserve leading zeroes')
        symbols[code] = name
    if len(symbols) != 100:
        raise ValueError(f'Expected 100 KOSPI100 constituents, got {len(symbols)}')
    return symbols, raw


# 공식 거래일 목록에서 수집 기간에 해당하는 날짜만 고른다.
def trading_dates(path, start, end):
    with path.open(encoding='utf-8-sig', newline='') as handle:
        dates = [dt.date.fromisoformat(row['date']) for row in csv.DictReader(handle)]
    if len(dates) != len(set(dates)):
        raise ValueError('Duplicate trading dates')
    selected = sorted(d for d in dates if start <= d <= end)
    if not selected:
        raise ValueError('No official trading dates in requested range')
    return selected


# KRX 응답에서 대상 종목을 골라 가격을 검사하고 누락을 기록한다.
def parse_krx(payload, day, symbols, collected):
    response = json.loads(payload)
    if not isinstance(response, dict) or not isinstance(response.get('OutBlock_1'), list):
        raise ValueError('Unexpected KRX response (check API approval/key)')
    found, issues, seen = {}, {}, set()
    for row in response['OutBlock_1']:
        code = row['ISU_CD']
        if code not in symbols:
            continue
        if code in seen:
            raise ValueError('Duplicate KRX symbol for date')
        seen.add(code)
        if row['BAS_DD'] != day.strftime('%Y%m%d'):
            raise ValueError('KRX returned the wrong trading date')
        try:
            op, hi, lo, cl, vol = (int(str(row[k]).replace(',', '')) for k in
                ('TDD_OPNPRC', 'TDD_HGPRC', 'TDD_LWPRC', 'TDD_CLSPRC', 'ACC_TRDVOL'))
        except (ValueError, KeyError):
            issues[code] = 'missing_or_invalid_ohlcv'
            continue
        if vol < 0 or min(op, hi, lo, cl) < 0 or (min(op, hi, lo, cl) == 0 and vol != 0):
            issues[code] = 'invalid_price_or_volume'
            continue
        if min(op, hi, lo, cl) > 0 and not (lo <= min(op, cl) <= max(op, cl) <= hi):
            issues[code] = 'invalid_price_range'
            continue
        found[code] = dict(zip(FIELDS, (code, day.isoformat(), op, hi, lo, cl, vol,
                                       'KRX_OPEN_API', 'provider_as_received', collected)))
    return list(found.values()), issues, sorted(set(symbols) - seen)


# 하루치 원본을 받는다. 완전히 수집된 날짜는 재실행 시 재사용한다.
def collect_day(day, symbols, root, key, refresh):
    stem = day.isoformat()
    cache = root / 'krx-cache' / f'{stem}.json'
    if cache.exists() and not refresh:
        meta = json.loads(cache.read_text(encoding='utf-8'))
        payload = (root / meta['file']).read_bytes()
        if hashlib.sha256(payload).hexdigest() != meta['sha256']:
            raise ValueError('KRX raw checksum mismatch')
    else:
        payload = legacy.request(KRX, {'basDd': day.strftime('%Y%m%d')}, 0.3, {'AUTH_KEY': key})
        digest = hashlib.sha256(payload).hexdigest()
        rel = Path('raw/krx') / str(day.year) / stem / f'{digest}.json'
        atomic_bytes(root / rel, payload)
        meta = dict(file=rel.as_posix(), sha256=digest,
                    collected_at=dt.datetime.now(dt.timezone.utc).isoformat())
    rows, issues, missing = parse_krx(payload, day, symbols, meta['collected_at'])
    if rows and not issues and not missing:
        legacy.write_json(cache, meta)
    return rows, issues, missing, meta


# 연간 재무제표는 연결(CFS)을 먼저 받고 없을 때만 별도(OFS)를 받는다.
def annual(code, corp, year, root, key, refresh):
    for scope in ('CFS', 'OFS'):
        cache = root / 'dart-cache' / code / f'{year}-{scope}.json'
        if cache.exists() and not refresh:
            meta = json.loads(cache.read_text(encoding='utf-8'))
            payload = (root / meta['file']).read_bytes()
            if hashlib.sha256(payload).hexdigest() != meta['sha256']:
                raise ValueError('OpenDART raw checksum mismatch')
        else:
            payload = legacy.request(legacy.DART + 'fnlttSinglAcntAll.json', dict(
                crtfc_key=key, corp_code=corp, bsns_year=year, reprt_code='11011', fs_div=scope), 0.3)
        response = json.loads(payload)
        if response.get('status') == '013':  # 자료 없음. 다른 재무제표 구분을 확인한다.
            continue
        if response.get('status') != '000':
            status = response.get('status')
            raise ValueError(f'OpenDART status {status}' if isinstance(status, str) and re.fullmatch(r'\d{3}', status) else 'Unexpected OpenDART response')
        rows = response['list']
        receipts = {row['rcept_no'] for row in rows}
        if len(receipts) != 1 or not re.fullmatch(r'\d{14}', receipt := next(iter(receipts))):
            raise ValueError('Unexpected OpenDART receipt')
        digest = hashlib.sha256(payload).hexdigest()
        rel = Path('raw/dart') / code / str(year) / f'{receipt}-{scope}-{digest}.json'
        atomic_bytes(root / rel, payload)
        meta = dict(file=rel.as_posix(), sha256=digest, receipt_no=receipt, scope=scope)
        fields = sorted({k for row in rows for k in row})
        atomic_bytes(root / 'annual' / code / str(year) / f'{receipt}-{scope}.csv',
                     csv_bytes([{k: row.get(k, '') for k in fields} for row in rows], fields))
        legacy.write_json(cache, meta)
        return dict(meta, rows=len(rows), status='collected', vintage='latest_available')
    return dict(status='no_data')


# 실행 옵션을 확인하고 종목·기간별 수집을 진행한다.
def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('kind', choices=('prices', 'financials'))
    cli.add_argument('--universe', required=True, type=Path)
    cli.add_argument('--calendar', type=Path)
    cli.add_argument('--env-file', type=Path, help='Read only the key for this collection from an existing .env file')
    cli.add_argument('--start', default='2021-01-01', type=dt.date.fromisoformat)
    cli.add_argument('--end', default='2025-12-31', type=dt.date.fromisoformat)
    cli.add_argument('--first-financial-year', type=int, default=2019)
    cli.add_argument('--output', type=Path, default=legacy.BASE / 'data' / 'final-design')
    cli.add_argument('--refresh', action='store_true')
    args = cli.parse_args()
    if args.start > args.end or args.first_financial_year > args.end.year:
        cli.error('Invalid collection range')
    if args.kind == 'prices' and not args.calendar:
        cli.error('Supply --calendar from an official KRX trading-day export')
    key_name = 'KRX_API_KEY' if args.kind == 'prices' else 'DART_API_KEY'
    if not (key := api_key(key_name, args.env_file)):
        cli.error(f'Set {key_name} in the current terminal or pass --env-file')
    # 사용한 종목 목록과 해시를 남겨 수집 기준을 추적한다.
    symbols, raw = snapshot(args.universe)
    root = args.output
    digest = hashlib.sha256(raw).hexdigest()
    frozen = root / 'universe' / f'{AS_OF}-{digest}.csv'
    atomic_bytes(frozen, raw)
    report = dict(kind=args.kind, universe_as_of=AS_OF, universe_sha256=digest,
                  universe_file=str(frozen), start=str(args.start), end=str(args.end), results=[])
    if args.kind == 'prices':
        work = [(day,) for day in trading_dates(args.calendar, args.start, args.end)]
    else:
        mapping = legacy.corp_mapping(root, key, 0.3, args.refresh)
        work = [(code, year) for code in sorted(symbols)
                for year in range(args.first_financial_year, args.end.year + 1)]
    log_time = dt.datetime.now(dt.timezone.utc)
    log = root / 'runs' / f'{log_time:%Y%m%dT%H%M%S%f}-{args.kind}.json'
    # 날짜별 가격 또는 종목·연도별 재무제표를 수집하고 결과를 기록한다.
    for item in work:
        result = dict(target=list(map(str, item)))
        try:
            if args.kind == 'prices':
                day = item[0]
                rows, issues, missing, meta = collect_day(day, symbols, root, key, args.refresh)
                atomic_bytes(root / 'prices' / str(day.year) / f'{day}.csv', csv_bytes(rows, FIELDS))
                result.update(meta, rows=len(rows), missing=missing, invalid=issues,
                              zero_volume=sum(r['volume'] == 0 for r in rows),
                              status='no_data' if not rows and not issues else 'partial' if issues or missing else 'collected')
            else:
                code, year = item
                result.update(annual(code, mapping[code], year, root, key, args.refresh))
        except (ValueError, KeyError, TypeError, RuntimeError, OSError) as exc:
            result.update(status='error', error=str(exc))
        report['results'].append(result)
        legacy.write_json(log, report)
        if result.get('error') in ('HTTP 401', 'HTTP 403', 'OpenDART status 010', 'OpenDART status 011', 'OpenDART status 012', 'OpenDART status 020'):
            break
    # 성공·누락·오류 건수와 저장 용량을 실행 보고서에 남긴다.
    counts = {s: sum(r['status'] == s for r in report['results']) for s in ('collected', 'partial', 'no_data', 'error')}
    report.update(counts=counts, unattempted=len(work) - len(report['results']),
                  rows=sum(r.get('rows', 0) for r in report['results']),
                  bytes_by_folder={folder: sum(f.stat().st_size for f in (root / folder).rglob('*') if f.is_file())
                                   for folder in ('raw', 'prices', 'annual')})
    legacy.write_json(log, report)
    print(log, counts)
    return 2 if any(counts[s] for s in ('partial', 'no_data', 'error')) else 0


# 이 파일을 직접 실행할 때만 수집을 시작한다.
if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, RuntimeError, KeyError, OSError) as exc:
        sys.exit(f'Collection setup failed: {exc}')
