import unittest
from pathlib import Path
from unittest.mock import patch
import collector as c


class CollectorTests(unittest.TestCase):
    def row(self, **changes):
        return dict(dict(srtnCd='005930', mrktCtg='KOSPI', basDt='20250102',
                         mkp='100', hipr='120', lopr='90', clpr='110', trqu='123'), **changes)

    def test_prices_sort_and_deduplicate(self):
        rows = c.normalize_prices([self.row(basDt='20250103'), self.row(), self.row()], '005930', 2025)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['date'], '2025-01-02')
        self.assertEqual(rows[0]['volume'], 123)

    def test_reject_bad_price_data(self):
        for changes in [dict(clpr='200'), dict(srtnCd='000660'), dict(basDt='20260102'), dict(trqu='-1')]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                c.normalize_prices([self.row(**changes)], '005930', 2025)

    def test_dart_fallback_only_on_missing(self):
        missing = {'status': '013'}
        good = {'status': '000', 'list': [{'rcept_no': '20260301000001', 'thstrm_amount': '10', 'thstrm_add_amount': '30'}]}
        with patch.object(c, 'cached_json', side_effect=[missing, good] * 4):
            rows, absent = c.financials('005930', '00126380', 2025, Path('.'), 'secret', 0, False)
        self.assertEqual(absent, [])
        self.assertEqual(rows[0]['fs_div'], 'OFS')
        self.assertEqual(rows[0]['filing_date'], '20260301')
        self.assertEqual(rows[0]['thstrm_add_amount'], '30')
        with patch.object(c, 'cached_json', return_value={'status': '020'}), self.assertRaises(RuntimeError):
            c.financials('005930', '00126380', 2025, Path('.'), 'secret', 0, False)

    def test_fsc_pagination(self):
        def response(items):
            return {'response': {'header': {'resultCode': '00'}, 'body': {'totalCount': 2, 'items': {'item': items}}}}
        with patch.object(c, 'cached_json', side_effect=[response([self.row()]), response([self.row(basDt='20250103')])]):
            rows = c.prices('005930', 2025, Path('.'), 'secret', 0, False)
        self.assertEqual(len(rows), 2)


if __name__ == '__main__':
    unittest.main()
