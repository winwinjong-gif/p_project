"""공시 접수번호별 재무제표 원본을 저장하고 과거 시점에 맞는 버전을 찾는다."""
import argparse  # 수집 기간과 파일 경로를 입력받는다.
import csv  # 거래일 목록을 읽는다.
import datetime as dt  # 공시일과 거래일을 비교한다.
import hashlib  # 원본 파일의 무결성을 확인한다.
import io  # API 응답이 ZIP인지 확인한다.
import json  # 공시와 저장 정보를 기록한다.
from pathlib import Path  # 파일 경로를 다룬다.
import re  # 공시번호와 보고서 연도를 검사한다.
import sys  # 실패 여부를 알린다.
import zipfile  # XBRL 원본 ZIP을 검사한다.

from auto_collect import disclosures
from collect_design import DART, api_key, atomic_bytes, corp_mapping, request, snapshot, write_json


# 설계서 기준: 공시일 다음 첫 거래일부터 자료를 쓴다.
def next_trading_day(published, calendar):
    return next((day for day in calendar if day > published), None)


# 각 접수번호의 XBRL 원본을 별도 보관한다. 최신 재무제표 API와 혼동하지 않는다.
def save_filing(filing, code, key, root, calendar):
    receipt = filing.get('rcept_no', '')
    if not re.fullmatch(r'\d{14}', receipt) or not re.fullmatch(r'[0-9A-Z]{6}', code):
        raise ValueError('종목코드 또는 접수번호 오류')
    match = re.search(r'\((\d{4})\.(\d{2})\)', filing.get('report_nm', ''))
    if not match:
        raise ValueError('사업보고서의 사업연도 확인 불가')
    published = dt.datetime.strptime(filing['rcept_dt'], '%Y%m%d').date()
    available = next_trading_day(published, calendar)
    # 접수일만 제공되므로 실제 공시 시각을 임의로 만들지 않는다.
    if available is None:
        raise ValueError('공시일 다음 거래일이 거래일 목록에 없음')
    folder = root / 'filings' / code / receipt
    metadata = folder / 'metadata.json'
    if metadata.exists():
        previous = json.loads(metadata.read_text(encoding='utf-8'))
        data = (folder / 'statement.zip').read_bytes()
        if hashlib.sha256(data).hexdigest() != previous['sha256']:
            raise ValueError('저장된 XBRL 원본 해시 오류')
        # 이후 거래일 목록이 변경되면 잘못된 사용가능일을 묵시적으로 유지하지 않는다.
        if previous['available_date'] != available.isoformat():
            raise ValueError('거래일 목록과 기존 사용가능일 불일치')
        return previous
    data = request(DART + 'fnlttXbrl.xml', {'crtfc_key': key, 'rcept_no': receipt,
                                            'reprt_code': '11011'}, 0.3)
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise ValueError('해당 접수번호의 XBRL 원본을 받지 못함')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if archive.testzip() or not archive.namelist():
            raise ValueError('XBRL ZIP 파일이 비었거나 손상됨')
    digest = hashlib.sha256(data).hexdigest()
    record = {'code': code, 'receipt_no': receipt, 'business_year': int(match.group(1)),
              'report_name': filing['report_nm'], 'published_date': published.isoformat(),
              'available_date': available.isoformat(), 'published_at': None,
              'sha256': digest, 'file': str(folder / 'statement.zip'),
              'status': 'raw_only', 'notice': '재무 수치 추출 및 원본 검증 필요'}
    atomic_bytes(folder / 'statement.zip', data)
    write_json(metadata, record)
    return record


# 특정 날짜에 이용 가능한 최신 연간 보고서를 선택한다.
def select_filing(records, code, as_of):
    eligible = [r for r in records if r['code'] == code and
                dt.date.fromisoformat(r['available_date']) <= as_of]
    if not eligible:
        return None
    # 과거 사업연도의 뒤늦은 정정이 더 최근 연도의 보고서를 밀어내지 않게 한다.
    return max(eligible, key=lambda r: (r['business_year'], r['published_date'], r['receipt_no']))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--universe', type=Path, required=True)
    parser.add_argument('--calendar', type=Path, required=True)
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parent / 'data' / 'final-design')
    parser.add_argument('--start', type=dt.date.fromisoformat, default=dt.date(2019, 1, 1))
    parser.add_argument('--end', type=dt.date.fromisoformat, default=dt.date(2025, 12, 31))
    args = parser.parse_args()
    if args.start > args.end:
        parser.error('시작일은 종료일보다 빨라야 합니다')
    key = api_key('DART_API_KEY', args.env_file)
    if not key:
        parser.error('DART_API_KEY가 필요합니다')
    symbols, raw = snapshot(args.universe)
    with args.calendar.open(encoding='utf-8-sig', newline='') as file:
        calendar = sorted(dt.date.fromisoformat(row['date']) for row in csv.DictReader(file))
    if len(calendar) != len(set(calendar)) or not calendar or calendar[-1] <= args.end:
        raise ValueError('중복 없는 거래일 목록과 종료일 이후 거래일이 필요합니다')
    mapping = corp_mapping(args.output, key, 0.3, False)
    codes = {corp: code for code, corp in mapping.items() if code in symbols}
    missing_codes = sorted(set(symbols) - set(codes.values()))
    report = {'start': str(args.start), 'end': str(args.end),
              'universe_sha256': hashlib.sha256(raw).hexdigest(),
              'missing_corp_codes': missing_codes, 'saved': [], 'errors': []}
    # 공시 검색은 전체 종목 조회 시 3개월 제한이 있어 60일씩 조회한다.
    cursor = args.start
    while cursor <= args.end:
        end = min(args.end, cursor + dt.timedelta(days=60))
        for filing in disclosures(key, cursor, end):
            if filing.get('corp_code') not in codes or '사업보고서' not in filing.get('report_nm', ''):
                continue
            try:
                record = save_filing(filing, codes[filing['corp_code']], key, args.output, calendar)
                report['saved'].append(record['receipt_no'])
            except (ValueError, KeyError, OSError, RuntimeError, zipfile.BadZipFile) as error:
                report['errors'].append({'receipt': filing.get('rcept_no'), 'error': str(error)})
        # 중간에 종료돼도 이전 보고서는 접수번호별로 저장돼 재실행할 수 있다.
        write_json(args.output / 'filings' / 'backfill_report.json', report)
        cursor = end + dt.timedelta(days=1)
    print('저장:', len(report['saved']), '오류:', len(report['errors']), '법인코드 누락:', len(missing_codes))
    return 1 if report['errors'] or missing_codes else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (RuntimeError, ValueError, KeyError, OSError) as error:
        sys.exit(f'공시 원본 수집 실패: {error}')
