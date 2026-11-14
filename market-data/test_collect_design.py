import datetime as dt
import json
import io
import zipfile
from unittest import TestCase
from unittest.mock import patch
from pathlib import Path

from collect_design import annual, parse_krx
from auto_collect import disclosures
from filing_versions import next_trading_day, select_filing
from extract_xbrl import extract


class KRXTests(TestCase):
    day = dt.date(2025, 1, 2)

    def parse(self, **changes):
        row = dict(ISU_CD='005930', BAS_DD='20250102', TDD_OPNPRC='53,000',
                   TDD_HGPRC='54,000', TDD_LWPRC='52,000', TDD_CLSPRC='53,500', ACC_TRDVOL='100')
        row.update(changes)
        return parse_krx(json.dumps({'OutBlock_1': [row]}), self.day,
                         {'005930', '000660'}, '2025-01-02T10:00:00+00:00')

    def test_fields_and_missing_symbol(self):
        rows, invalid, missing = self.parse()
        self.assertEqual((rows[0]['code'], rows[0]['open'], rows[0]['price_basis']),
                         ('005930', 53000, 'provider_as_received'))
        self.assertEqual(invalid, {})
        self.assertEqual(missing, ['000660'])

    def test_zero_volume_is_not_missing(self):
        rows, invalid, _ = self.parse(TDD_OPNPRC='0', TDD_HGPRC='0', TDD_LWPRC='0', ACC_TRDVOL='0')
        self.assertEqual(rows[0]['volume'], 0)
        self.assertEqual(invalid, {})

    def test_wrong_date_is_not_cached_as_holiday(self):
        with self.assertRaises(ValueError):
            self.parse(BAS_DD='20250103')


class DARTTests(TestCase):
    def test_xbrl_distinguishes_scope_and_disputes(self):
        xml = '''<xbrl xmlns="http://www.xbrl.org/2003/instance"
          xmlns:d="http://xbrl.org/2006/xbrldi" xmlns:f="http://xbrl.ifrs.org/taxonomy/2021-03-24/ifrs-full">
          <context id="con"><entity><identifier scheme="x">1</identifier><segment>
            <d:explicitMember dimension="f:ConsolidatedAndSeparateFinancialStatementsAxis">f:ConsolidatedMember</d:explicitMember>
          </segment></entity><period><instant>2024-12-31</instant></period></context>
          <context id="sub"><entity><identifier scheme="x">1</identifier><segment>
            <d:explicitMember dimension="f:ConsolidatedAndSeparateFinancialStatementsAxis">f:ConsolidatedMember</d:explicitMember>
            <d:explicitMember dimension="f:OtherAxis">f:OtherMember</d:explicitMember>
          </segment></entity><period><instant>2024-12-31</instant></period></context>
          <unit id="won"><measure>iso4217:KRW</measure></unit>
          <f:Assets contextRef="con" unitRef="won">100</f:Assets>
          <f:Assets contextRef="sub" unitRef="won">999</f:Assets>
          <f:Equity contextRef="con" unitRef="won">20</f:Equity>
          <f:Equity contextRef="con" unitRef="won">30</f:Equity>
        </xbrl>'''
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as z:
            z.writestr('test.xbrl', xml)
        data.seek(0)
        with patch('extract_xbrl.zipfile.ZipFile', return_value=zipfile.ZipFile(data)):
            result = extract('unused.zip', dt.date(2024, 12, 31), 'CFS')
        self.assertEqual(result['Assets']['amount'], '100')
        self.assertEqual(result['Equity']['status'], 'conflict')
        self.assertEqual(result['ProfitLoss']['status'], 'missing')

    def test_version_selection_respects_publication_and_fiscal_year(self):
        reports = [
            dict(code='005930', receipt_no='20210301000001', business_year=2020,
                 published_date='2021-03-01', available_date='2021-03-02'),
            dict(code='005930', receipt_no='20210810000001', business_year=2020,
                 published_date='2021-08-10', available_date='2021-08-11'),
            dict(code='005930', receipt_no='20220301000001', business_year=2021,
                 published_date='2022-03-01', available_date='2022-03-02'),
            dict(code='005930', receipt_no='20220401000001', business_year=2020,
                 published_date='2022-04-01', available_date='2022-04-04'),
        ]
        self.assertIsNone(select_filing(reports, '005930', dt.date(2021, 3, 1)))
        self.assertEqual(select_filing(reports, '005930', dt.date(2021, 5, 1))['receipt_no'], '20210301000001')
        self.assertEqual(select_filing(reports, '005930', dt.date(2021, 9, 1))['receipt_no'], '20210810000001')
        self.assertEqual(select_filing(reports, '005930', dt.date(2022, 5, 1))['business_year'], 2021)

    def test_next_trading_day_is_strictly_after_disclosure(self):
        days = [dt.date(2025, 1, 3), dt.date(2025, 1, 6)]
        self.assertEqual(next_trading_day(days[0], days), days[1])
        self.assertIsNone(next_trading_day(days[1], days))

    def test_disclosures_fetches_all_pages_and_corrections(self):
        first = json.dumps({'status': '000', 'total_page': 2, 'list': [{'rcept_no': '1'}]}).encode()
        second = json.dumps({'status': '000', 'total_page': 2, 'list': [{'rcept_no': '2'}]}).encode()
        with patch('auto_collect.request', side_effect=[first, second]) as mock_request:
            items = disclosures('key', dt.date(2025, 1, 1), dt.date(2025, 1, 2))
        self.assertEqual(len(items), 2)
        self.assertEqual(mock_request.call_args_list[0].args[1]['last_reprt_at'], 'N')

    def test_disclosures_rejects_missing_page(self):
        first = json.dumps({'status': '000', 'total_page': 2, 'list': [{'rcept_no': '1'}]}).encode()
        missing = json.dumps({'status': '013'}).encode()
        with patch('auto_collect.request', side_effect=[first, missing]), self.assertRaises(ValueError):
            disclosures('key', dt.date(2025, 1, 1), dt.date(2025, 1, 2))

    def test_fallback_to_separate_when_consolidated_missing(self):
        missing = json.dumps({'status': '013'}).encode()
        good = json.dumps({'status': '000', 'list': [
            {'rcept_no': '20260301000001', 'thstrm_amount': '10'}]}).encode()
        with patch('collect_design.request', side_effect=[missing, good]) as mock_request, \
             patch('collect_design.atomic_bytes'), patch('collect_design.write_json'):
            result = annual('005930', '00126380', 2025, Path('unused'), 'key', True)
        self.assertEqual(result['scope'], 'OFS')
        self.assertEqual(mock_request.call_count, 2)

    def test_auth_error_does_not_fallback(self):
        bad = json.dumps({'status': '010'}).encode()
        with patch('collect_design.request', return_value=bad) as mock_request:
            with self.assertRaisesRegex(ValueError, '010'):
                annual('005930', '00126380', 2025, Path('unused'), 'key', True)
        self.assertEqual(mock_request.call_count, 1)
