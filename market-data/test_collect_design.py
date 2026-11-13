import datetime as dt
import json
from unittest import TestCase

from collect_design import parse_krx


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
