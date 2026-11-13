import datetime as dt
import json
from unittest import TestCase
from unittest.mock import patch
from pathlib import Path

from collect_design import annual, parse_krx
from auto_collect import disclosures


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
