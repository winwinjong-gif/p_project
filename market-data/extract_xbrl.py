"""연간 XBRL 원본에서 확인 가능한 표준 재무 계정을 추출한다."""
import argparse  # 파일과 보고기간을 입력받는다.
import datetime as dt  # 재무제표 기간을 확인한다.
from decimal import Decimal, InvalidOperation  # 금액을 오차 없이 읽는다.
import json  # 추출 결과를 저장한다.
from pathlib import Path  # 파일 경로를 다룬다.
import sys  # 오류를 실행 결과로 알린다.
import xml.etree.ElementTree as ET  # XBRL의 계정과 기간을 읽는다.
import zipfile  # OpenDART 원본 ZIP을 읽는다.

from collect_design import write_json

XBRL = '{http://www.xbrl.org/2003/instance}'
DIM = '{http://xbrl.org/2006/xbrldi}'
XSI = '{http://www.w3.org/2001/XMLSchema-instance}'
ACCOUNTS = {
    'Assets': 'instant',
    'Equity': 'instant',
    'EquityAttributableToOwnersOfParent': 'instant',
    'Revenue': 'duration',
    'ProfitLoss': 'duration',
    'ProfitLossAttributableToOwnersOfParent': 'duration',
}


# 세부 주석 축은 제외하고 연결 또는 별도 재무제표만 선택한다.
def extract(path, end_date, scope):
    with zipfile.ZipFile(path) as archive:
        files = [name for name in archive.namelist() if name.lower().endswith('.xbrl')]
        if len(files) != 1:
            raise ValueError('XBRL 본문 파일이 하나가 아닙니다')
        root = ET.fromstring(archive.read(files[0]))
    contexts = {node.get('id'): node for node in root.findall(XBRL + 'context')}
    units = {node.get('id'): node for node in root.findall(XBRL + 'unit')}
    candidates = {name: [] for name in ACCOUNTS}
    wanted_member = 'ConsolidatedMember' if scope == 'CFS' else 'SeparateMember'
    for fact in root:
        namespace, _, name = fact.tag.rpartition('}')
        if name not in ACCOUNTS or not namespace.startswith('{http://xbrl.ifrs.org/taxonomy/'):
            continue
        context = contexts.get(fact.get('contextRef'))
        unit = units.get(fact.get('unitRef'))
        if context is None or unit is None or fact.get(XSI + 'nil') == 'true':
            continue
        dimensions = context.findall('.//' + DIM + 'explicitMember')
        # 다른 축이 있으면 주석의 부분 금액일 수 있으므로 제외한다.
        if len(dimensions) != 1 or context.findall('.//' + DIM + 'typedMember'):
            continue
        axis = dimensions[0].get('dimension', '').split(':')[-1]
        member = (dimensions[0].text or '').split(':')[-1]
        if axis != 'ConsolidatedAndSeparateFinancialStatementsAxis' or member != wanted_member:
            continue
        measures = unit.findall(XBRL + 'measure')
        if len(measures) != 1 or (measures[0].text or '').split(':')[-1] != 'KRW':
            continue
        period = context.find(XBRL + 'period')
        if period is None:
            continue
        end_text = period.findtext(XBRL + ('instant' if ACCOUNTS[name] == 'instant' else 'endDate'))
        if end_text != end_date.isoformat():
            continue
        if ACCOUNTS[name] == 'duration':
            try:
                start = dt.date.fromisoformat(period.findtext(XBRL + 'startDate', ''))
            except ValueError:
                continue
            if not 350 <= (end_date - start).days + 1 <= 380:
                continue
        try:
            amount = Decimal((fact.text or '').strip())
            if not amount.is_finite():
                continue
        except InvalidOperation:
            continue
        candidates[name].append({'amount': str(amount), 'context': fact.get('contextRef'),
                                 'unit': 'KRW', 'decimals': fact.get('decimals')})
    # 중복 수치가 일치하면 하나로 보관하고 충돌하면 임의로 고르지 않는다.
    result = {}
    for name, values in candidates.items():
        amounts = {Decimal(value['amount']) for value in values}
        if len(amounts) == 1:
            result[name] = {'status': 'ok', **values[0]}
        else:
            result[name] = {'status': 'missing' if not values else 'conflict', 'candidates': values}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('zip_path', type=Path)
    parser.add_argument('--period-end', type=dt.date.fromisoformat, required=True)
    parser.add_argument('--scope', choices=('CFS', 'OFS'), default='CFS')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    result = extract(args.zip_path, args.period_end, args.scope)
    write_json(args.out, {'source_file': str(args.zip_path), 'period_end': str(args.period_end),
                          'scope': args.scope, 'accounts': result})
    print('확인된 항목:', sum(item['status'] == 'ok' for item in result.values()), '/', len(result))
    return 0 if all(item['status'] == 'ok' for item in result.values()) else 2


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, OSError, ET.ParseError, zipfile.BadZipFile) as error:
        sys.exit(f'XBRL 추출 실패: {error}')
