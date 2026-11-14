"""거래일 가격과 새 연간 공시를 주기적으로 수집한다."""
import argparse  # 실행 옵션을 받는다.
import csv  # 거래일 목록을 읽는다.
import datetime as dt  # 날짜 범위를 계산한다.
import hashlib  # 종목 목록이 이전 실행과 같은지 확인한다.
import json  # 공시 API 응답을 읽는다.
from pathlib import Path  # 파일 경로를 다룬다.
import re  # 사업보고서의 사업연도를 읽는다.
import sys  # 실패를 스케줄러에 알린다.
from zoneinfo import ZoneInfo  # 한국 날짜를 기준으로 실행한다.

from collect_design import (DART, api_key, annual, atomic_bytes, collect_day,
                            corp_mapping, csv_bytes, FIELDS, request, snapshot,
                            trading_dates, write_json)


# 접수일 기준 최근 공시를 페이지 끝까지 조회한다.
def disclosures(key, start, end):
    page = 1
    items = []
    while True:
        params = dict(crtfc_key=key, bgn_de=start.strftime('%Y%m%d'),
                      end_de=end.strftime('%Y%m%d'), pblntf_ty='A',
                      pblntf_detail_ty='A001', last_reprt_at='N',
                      page_no=page, page_count=100)
        result = json.loads(request(DART + 'list.json', params, 0.3))
        if result.get('status') == '013':
            if page != 1:
                raise ValueError('공시 목록 페이지가 중간에 누락됨')
            return []
        if result.get('status') != '000':
            raise ValueError('OpenDART 공시 조회 오류')
        entries = result.get('list')
        total_pages = int(result['total_page'])
        if not isinstance(entries, list) or not entries or total_pages < page:
            raise ValueError('공시 목록 응답이 불완전함')
        items.extend(entries)
        if page >= total_pages:
            return items
        page += 1


# 매일 실행한다. 실패한 항목은 기록해 다음 실행에서 다시 시도한다.
def main():
    from filing_versions import save_filing  # 공시별 원본 파일을 보관한다.
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--universe', required=True, type=Path)
    cli.add_argument('--calendar', required=True, type=Path)
    cli.add_argument('--env-file', type=Path)
    cli.add_argument('--output', type=Path, default=Path(__file__).resolve().parent / 'data' / 'final-design')
    cli.add_argument('--price-start', type=dt.date.fromisoformat, default=dt.date(2021, 1, 1))
    cli.add_argument('--today', type=dt.date.fromisoformat, help='테스트용 한국 날짜 (YYYY-MM-DD)')
    args = cli.parse_args()
    today = args.today or dt.datetime.now(ZoneInfo('Asia/Seoul')).date()
    # 당일 가격은 확정 여부가 불분명하므로 전날까지 요청한다.
    price_end = today - dt.timedelta(days=1)
    if args.price_start > price_end:
        cli.error('주가 수집 시작일이 종료일보다 늦음')
    keys = {name: api_key(name, args.env_file) for name in ('KRX_API_KEY', 'DART_API_KEY')}
    if not all(keys.values()):
        cli.error('KRX_API_KEY와 DART_API_KEY가 필요함')
    symbols, universe_raw = snapshot(args.universe)
    universe_hash = hashlib.sha256(universe_raw).hexdigest()
    root = args.output
    state_path = root / 'automation' / 'state.json'
    state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
    if state and state.get('universe_sha256') != universe_hash:
        raise ValueError('종목 목록이 이전 실행과 다름. 별도 output 폴더를 사용하세요.')
    state.setdefault('universe_sha256', universe_hash)
    state.setdefault('pending', {})
    state.setdefault('completed', {})
    errors = []

    # 과거 거래일도 매번 확인한다. 완전히 수집된 날짜는 원본 캐시를 사용한다.
    days = trading_dates(args.calendar, args.price_start, price_end)
    calendar_dates = sorted(dt.date.fromisoformat(row['date']) for row in csv.DictReader(
        args.calendar.read_text(encoding='utf-8-sig').splitlines()))
    if max(calendar_dates) < price_end - dt.timedelta(days=7):
        raise ValueError('거래일 목록이 최신이 아닙니다. 공식 목록을 갱신하세요.')
    for day in days:
        try:
            rows, invalid, missing, meta = collect_day(day, symbols, root, keys['KRX_API_KEY'], False)
            atomic_bytes(root / 'prices' / str(day.year) / f'{day}.csv', csv_bytes(rows, FIELDS))
            if invalid or missing or not rows:
                errors.append(dict(dataset='prices', date=str(day), invalid=invalid, missing=missing))
        except (RuntimeError, ValueError, KeyError, OSError) as exc:
            errors.append(dict(dataset='prices', date=str(day), error=str(exc)))

    # 빠진 실행일이 있어도 마지막 성공일부터 현재까지 모든 공시를 조회한다.
    last = state.get('last_disclosure_date')
    start = dt.date.fromisoformat(last) if last else today - dt.timedelta(days=7)
    if start > today:
        raise ValueError('마지막 공시 확인일이 현재 날짜보다 늦습니다.')
    mapping = corp_mapping(root, keys['DART_API_KEY'], 0.3, False)
    codes = {corp: code for code, corp in mapping.items() if code in symbols}
    cursor = start
    while cursor <= today:
        end = min(today, cursor + dt.timedelta(days=60))
        found = disclosures(keys['DART_API_KEY'], cursor, end)
        for item in found:
            corp, receipt = item.get('corp_code'), item.get('rcept_no', '')
            if corp not in codes or '사업보고서' not in item.get('report_nm', ''):
                continue
            if not re.fullmatch(r'\d{14}', receipt):
                raise ValueError('공시 접수번호 형식 오류')
            if receipt not in state['completed']:
                state['pending'][receipt] = item
        # 다음 실행에서도 이 구간을 재확인한다.
        state['last_disclosure_date'] = end.isoformat()
        write_json(state_path, state)
        cursor = end + dt.timedelta(days=1)

    for receipt, filing in list(state['pending'].items()):
        try:
            match = re.search(r'\((\d{4})\.\d{2}\)', filing.get('report_nm', ''))
            if not match:
                raise ValueError('보고서에서 사업연도를 확인할 수 없음')
            code = codes[filing['corp_code']]
            # 최신 수치 조회와 별개로 접수번호별 XBRL 원본을 먼저 보관한다.
            original = save_filing(filing, code, keys['DART_API_KEY'], root, calendar_dates)
            result = annual(code, filing['corp_code'], int(match.group(1)), root, keys['DART_API_KEY'], True)
            if result['status'] != 'collected':
                raise ValueError('재무제표가 아직 제공되지 않음')
            if result['receipt_no'] != receipt:
                raise ValueError('최신 재무제표의 접수번호가 달라 해당 공시 버전 확인 필요')
            state['completed'][receipt] = dict(filing=filing, financial=result, original=original)
            del state['pending'][receipt]
            write_json(state_path, state)
        except (RuntimeError, ValueError, KeyError, OSError) as exc:
            errors.append(dict(dataset='financials', receipt=receipt, error=str(exc)))
    run_time = dt.datetime.now(dt.timezone.utc)
    report_path = root / 'automation' / 'runs' / f'{run_time:%Y%m%dT%H%M%S%f}.json'
    write_json(report_path, dict(date=today.isoformat(), price_days=len(days), errors=errors,
                                 pending=len(state['pending'])))
    print(f'실행 기록: {report_path}, 오류: {len(errors)}, 처리 대기 공시: {len(state["pending"])}')
    return 1 if errors or state['pending'] else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (RuntimeError, ValueError, OSError, KeyError) as error:
        sys.exit(f'자동 수집 실패: {error}')
