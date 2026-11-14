"""실제 DB 없이 적재 전 데이터 검사와 SQL 인자를 확인한다."""
import datetime as dt
import hashlib
import json
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, mock_open, patch

from load_postgres import load_financial, load_prices, load_stocks


class LoadTests(TestCase):
    def setUp(self):
        self.connection = MagicMock()
        self.cursor = self.connection.cursor.return_value.__enter__.return_value
        self.universe = {'005930': '삼성전자'}
        self.header = 'code,date,open,high,low,close,volume,source,price_basis,collected_at\n'
        self.row = '005930,2025-01-02,100,120,90,110,7,KRX_OPEN_API,provider_as_received,2025-01-03T00:00:00+09:00\n'

    def test_stock_code_and_date_are_parameters(self):
        self.assertEqual(load_stocks(self.connection, self.universe), 1)
        self.assertEqual(self.cursor.execute.call_args.args[1], ('005930', '삼성전자', '2025-01-02'))

    def test_prices_preserves_code_and_converts_time_to_utc(self):
        with patch.object(Path, 'open', mock_open(read_data=self.header + self.row)):
            self.assertEqual(load_prices(self.connection, [Path('prices.csv')], self.universe), 1)
        sql, params = self.cursor.execute.call_args.args
        self.assertIn('ON CONFLICT (code, date)', sql)
        self.assertEqual(params[0], '005930')
        self.assertEqual(params[-1], dt.datetime(2025, 1, 2, 15))

    def test_reject_duplicate_and_out_of_range_prices(self):
        for data in (self.header + self.row + self.row,
                     self.header + self.row.replace(',100,120,90,110,', ',100,105,90,110,')):
            with self.subTest(data=data), patch.object(Path, 'open', mock_open(read_data=data)):
                with self.assertRaises(ValueError):
                    load_prices(self.connection, [Path('prices.csv')], self.universe)

    def test_financial_versions_use_distinct_report_ids(self):
        payload = b'example archive'
        accounts = {'Equity': {'status': 'ok', 'amount': '100', 'unit': 'KRW'},
                    'ProfitLoss': {'status': 'missing'}}
        self.cursor.fetchone.return_value = ('new',)
        ids = []
        for receipt in ('20250321001912', '20250325000742'):
            meta = {'code': '005930', 'receipt_no': receipt, 'published_date': '2025-03-25',
                    'available_date': '2025-03-26', 'report_name': '사업보고서 (2024.12)',
                    'business_year': 2024, 'sha256': hashlib.sha256(payload).hexdigest()}
            with patch.object(Path, 'read_text', return_value=json.dumps(meta)), \
                 patch.object(Path, 'read_bytes', return_value=payload), \
                 patch('load_postgres.extract', return_value=accounts):
                count, missing = load_financial(self.connection, Path('metadata.json'),
                    dt.datetime.fromisoformat('2025-03-25T16:00:00+09:00'), 'CFS', self.universe)
            self.assertEqual((count, missing), (1, ['ProfitLoss']))
            ids.append(self.cursor.execute.call_args_list[-2].args[1][0])
        self.assertNotEqual(*ids)

    def test_financial_rejects_fake_time_and_bad_hash(self):
        meta = {'code': '005930', 'receipt_no': '20250325000742', 'published_date': '2025-03-25',
                'available_date': '2025-03-26', 'report_name': '사업보고서 (2024.12)',
                'business_year': 2024, 'sha256': 'wrong'}
        with patch.object(Path, 'read_text', return_value=json.dumps(meta)), \
             patch.object(Path, 'read_bytes', return_value=b'archive'):
            with self.assertRaises(ValueError):
                load_financial(self.connection, Path('metadata.json'), dt.datetime(2025, 3, 25), 'CFS', self.universe)
            with self.assertRaises(ValueError):
                load_financial(self.connection, Path('metadata.json'),
                    dt.datetime.fromisoformat('2025-03-25T16:00:00+09:00'), 'CFS', self.universe)
