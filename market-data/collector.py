"""초기 수집기와 최종 수집기에서 함께 쓰는 API 요청 기능."""
import argparse  # 실행 옵션을 읽는다.
import csv  # 종목 목록을 읽는다.
import datetime as dt  # 날짜와 수집 시각을 처리한다.
import hashlib  # 종목 목록의 해시를 기록한다.
import io  # 다운로드한 ZIP 데이터를 메모리에서 읽는다.
import json  # API 응답과 실행 결과를 다룬다.
import os  # 환경변수의 인증키를 읽는다.
from pathlib import Path  # 파일 경로를 다룬다.
import re  # 종목코드 형식을 확인한다.
import time  # 요청 간격과 재시도 대기시간을 둔다.
import urllib.error  # HTTP와 연결 오류를 구분한다.
import urllib.parse  # API 요청 인자를 URL 형식으로 변환한다.
import urllib.request  # 외부 API에 요청을 보낸다.
import xml.etree.ElementTree as ET  # OpenDART 법인코드 XML을 읽는다.
import zipfile  # 법인코드 ZIP 압축을 읽는다.

BASE = Path(__file__).resolve().parent
FSC = 'https://apis.data.go.kr/1160100/service/GetStockSecuritiesInfoService/getStockPriceInfo'
DART = 'https://opendart.fss.or.kr/api/'
REPORTS = ('11013', '11012', '11014', '11011')


# 실행 결과를 임시 파일에 먼저 쓴 후 기존 파일을 교체한다.
def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


# API 요청을 보내고 일시적인 오류는 최대 4회 시도한다.
def request(url, params, interval, headers=None):
    # 요청 URL에는 키가 들어갈 수 있으므로 오류에 출력하지 않는다.
    for attempt in range(4):
        time.sleep(interval)
        try:
            req = urllib.request.Request(url + '?' + urllib.parse.urlencode(params),
                                         headers={'User-Agent': 'MoneyRehearsalResearch/0.1', **(headers or {})})
            with urllib.request.urlopen(req, timeout=45) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504):
                raise RuntimeError(f'HTTP {exc.code}') from None
        except (urllib.error.URLError, TimeoutError):
            pass
        time.sleep(2 ** attempt)
    raise RuntimeError('Network request failed after 4 attempts')


def cached_json(path, url, params, interval, refresh=False):
    if path.exists() and not refresh:
        return json.loads(path.read_text(encoding='utf-8'))
    try:
        value = json.loads(request(url, params, interval))
    except (ValueError, UnicodeError):
        raise RuntimeError('Provider returned a non-JSON response') from None
    # Cache successful responses only; missing reports must be retried on future runs.
    success = value.get('status') == '000' or str(value.get('response', {}).get('header', {}).get('resultCode')) in ('00', '0')
    if success:
        write_json(path, value)
    return value


def load_universe(config):
    if not config.get('universe_as_of') or not config.get('universe_source'):
        raise ValueError('Set universe_as_of and universe_source using a verified KOSPI100 constituent snapshot')
    dt.date.fromisoformat(config['universe_as_of'])
    with (BASE / config['universe_file']).open(encoding='utf-8-sig', newline='') as file:
        rows = list(csv.DictReader(file))
    codes = [r['symbol'] for r in rows]
    if len(codes) != config['expected_symbols'] or len(set(codes)) != len(codes):
        raise ValueError('Universe must contain exactly 100 unique symbols')
    if any(not re.fullmatch(r'[0-9A-Z]{6}', code) for code in codes):
        raise ValueError('Symbols must be 6-character strings; preserve leading zeroes')
    return rows


def normalize_prices(items, symbol, year):
    result = {}
    for row in items:
        if row['srtnCd'] != symbol or row['mrktCtg'] != 'KOSPI':
            raise ValueError('Unexpected symbol or market in price response')
        date = dt.datetime.strptime(row['basDt'], '%Y%m%d').date()
        if date.year != year:
            raise ValueError('Out-of-range price date')
        values = {key: int(row[field]) for key, field in
                  [('open', 'mkp'), ('high', 'hipr'), ('low', 'lopr'), ('close', 'clpr'), ('volume', 'trqu')]}
        if any(v < 0 for v in values.values()):
            raise ValueError('Negative OHLCV value')
        if values['high'] and not (values['low'] <= values['open'] <= values['high'] and values['low'] <= values['close'] <= values['high']):
            raise ValueError('Invalid OHLC relationship')
        item = dict(symbol=symbol, date=date.isoformat(), **values,
                    source='FSC', adjustment='unverified')
        if date in result and result[date] != item:
            raise ValueError('Conflicting duplicate price date')
        result[date] = item
    return [result[d] for d in sorted(result)]


def prices(symbol, year, root, key, interval, refresh):
    items, page, total = [], 1, None
    while total is None or len(items) < total:
        value = cached_json(root / 'raw' / 'fsc' / str(year) / symbol / f'{page}.json', FSC,
            dict(serviceKey=urllib.parse.unquote(key), resultType='json', numOfRows=1000,
                 pageNo=page, srtnCd=symbol, beginBasDt=f'{year}0101', endBasDt=f'{year}1231'), interval, refresh)
        response = value.get('response', {})
        if str(response.get('header', {}).get('resultCode')) not in ('00', '0'):
            raise RuntimeError('FSC error code ' + str(response.get('header', {}).get('resultCode')))
        body = response['body']
        current_total = int(body['totalCount'])
        if total is not None and total != current_total:
            raise RuntimeError('Pagination total changed; rerun with --refresh')
        total = current_total
        container = body.get('items') or {}
        batch = container.get('item', []) if isinstance(container, dict) else []
        if isinstance(batch, dict):
            batch = [batch]
        if not batch and len(items) < total:
            raise RuntimeError('Incomplete pagination')
        items.extend(batch)
        page += 1
    rows = normalize_prices(items, symbol, year)
    if not rows:
        raise RuntimeError('No price data returned')
    return rows


# OpenDART 법인코드 파일에서 종목코드와 법인코드를 연결한다.
def corp_mapping(root, key, interval, refresh):
    path = root / 'raw' / 'dart' / 'corpCode.zip'
    if refresh or not path.exists():
        payload = request(DART + 'corpCode.xml', {'crtfc_key': key}, interval)
        if not zipfile.is_zipfile(io.BytesIO(payload)):
            raise RuntimeError('DART corpCode request did not return a ZIP file')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    with zipfile.ZipFile(path) as archive:
        tree = ET.fromstring(archive.read('CORPCODE.xml'))
    return {node.findtext('stock_code').strip(): node.findtext('corp_code')
            for node in tree.findall('list') if node.findtext('stock_code', '').strip()}


def financials(symbol, corp, year, root, key, interval, refresh):
    rows, missing = [], []
    for report in REPORTS:
        for division in ('CFS', 'OFS'):
            value = cached_json(root / 'raw' / 'dart' / str(year) / symbol / f'{report}_{division}.json',
                DART + 'fnlttSinglAcntAll.json', dict(crtfc_key=key, corp_code=corp,
                bsns_year=year, reprt_code=report, fs_div=division), interval, refresh)
            if value.get('status') == '013':
                continue
            if value.get('status') != '000':
                raise RuntimeError('DART error code ' + str(value.get('status')))
            accounts = value.get('list', [])
            if not accounts:
                raise RuntimeError('DART success response has no accounts')
            for account in accounts:
                # Preserve account_detail, ord, currency, three-month and YTD amounts.
                receipt = account.get('rcept_no', '')
                rows.append(dict(account, symbol=symbol, fs_div=division, source='OpenDART',
                                 filing_date=receipt[:8], historical_vintage='latest_available'))
            break
        else:
            missing.append(report)
    return rows, missing


def save_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    with tmp.open('w', encoding='utf-8', newline='\n') as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n')
    tmp.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['plan', 'collect', 'report'])
    parser.add_argument('--only', choices=['prices', 'financials', 'all'], default='all')
    parser.add_argument('--refresh', action='store_true')
    args = parser.parse_args()
    config = json.loads((BASE / 'config.json').read_text(encoding='utf-8'))
    root = BASE / config['output_dir']
    year, interval = config['year'], config['request_interval_seconds']
    if args.command == 'plan':
        print(json.dumps(dict(config, estimated_price_rows='about 24,000-25,000; estimate only'), ensure_ascii=False, indent=2))
        return
    if args.command == 'report':
        files = list(root.rglob('*')) if root.exists() else []
        result = {folder: sum(p.stat().st_size for p in files if p.is_file() and folder in p.relative_to(root).parts)
                  for folder in ['raw', 'normalized']}
        result['jsonl_rows'] = {str(p.relative_to(root)): sum(1 for _ in p.open(encoding='utf-8'))
                                for p in files if p.suffix == '.jsonl'}
        write_json(root / 'size_report.json', result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    universe = load_universe(config)
    required = []
    if args.only in ('prices', 'all'): required.append('FSC_API_KEY')
    if args.only in ('financials', 'all'): required.append('DART_API_KEY')
    if any(not os.environ.get(key) for key in required):
        raise ValueError('Missing environment variables: ' + ', '.join(k for k in required if not os.environ.get(k)))
    mapping = corp_mapping(root, os.environ['DART_API_KEY'], interval, args.refresh) if 'DART_API_KEY' in required else {}
    run = dict(started_at=dt.datetime.now(dt.timezone.utc).isoformat(), config=config,
               universe_sha256=hashlib.sha256((BASE / config['universe_file']).read_bytes()).hexdigest(), results=[])
    run_path = root / 'runs' / (dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%f') + '.json')
    failures = 0
    for stock in universe:
        symbol = stock['symbol']
        for kind in ('prices', 'financials'):
            if args.only not in ('all', kind): continue
            result = dict(symbol=symbol, dataset=kind)
            try:
                if kind == 'prices':
                    rows = prices(symbol, year, root, os.environ['FSC_API_KEY'], interval, args.refresh)
                    result['zero_volume_days'] = sum(r['volume'] == 0 for r in rows)
                    result['first_date'], result['last_date'] = rows[0]['date'], rows[-1]['date']
                else:
                    if symbol not in mapping: raise ValueError('No DART corporation mapping')
                    rows, missing = financials(symbol, mapping[symbol], year, root, os.environ['DART_API_KEY'], interval, args.refresh)
                    result['missing_reports'] = missing
                save_jsonl(root / 'normalized' / str(year) / kind / f'{symbol}.jsonl', rows)
                result.update(status='partial' if result.get('missing_reports') else 'ok', rows=len(rows))
                if result['status'] == 'partial': failures += 1
            except (RuntimeError, ValueError, KeyError, OSError, ET.ParseError, zipfile.BadZipFile) as exc:
                failures += 1
                result.update(status='failed', error=str(exc))
            run['results'].append(result)
            write_json(run_path, run)
            print(symbol, kind, result['status'])
    print('Run log:', run_path)
    if failures: raise SystemExit(2)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError) as error:
        print('Error:', error)
        raise SystemExit(1)
