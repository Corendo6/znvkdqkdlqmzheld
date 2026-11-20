"""Daily comparison never mixes editable sales or incomplete API days."""
import sqlite3
import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import daily_comparison
import read_models
from test_daily_sales import MemoryDB, VENDOR


class DailyComparisonTests(unittest.TestCase):
    def setUp(self):
        self.conn = MemoryDB().conn
        self.addCleanup(self.conn.close)
        self.addCleanup(read_models.clear, self.conn)
        self.counter = 0

    def cover(self, *days, vendor=VENDOR, legacy=False):
        table = 'rg_sales_legacy_coverage' if legacy else 'rg_sales_coverage'
        self.conn.executemany(f'INSERT OR REPLACE INTO {table} VALUES (?,?,?)',
            [(vendor, day, '2026-11-20T09:00:00+09:00') for day in days])

    def raw(self, day, qty=1, revenue='1.1', oid='101', vendor=VENDOR, name='Raw name'):
        self.counter += 1
        self.conn.execute('INSERT INTO rg_order_items VALUES (?,?,?,?,?,?,?,?)',
            (vendor, str(self.counter), oid, day, name, qty, revenue, '2026-11-20T09:00:00+09:00'))

    def product(self, oid, name, option='Option', updated='2026-11-20', method='로켓그로스'):
        self.conn.execute('INSERT INTO products(option_id,fulfillment_type,model,option_name,product_name,updated_at) '
                          'VALUES (?,?,?,?,?,?)', (oid, method, 'SAME MODEL', option, name, updated))

    def report(self, base='2026-11-19', previous='2026-11-18', **kwargs):
        return daily_comparison.build_report(self.conn, VENDOR, base, previous,
                                              today=date(2026, 11, 20), **kwargs)

    def test_exact_option_union_decimal_totals_and_rankings(self):
        self.cover('2026-11-18', '2026-11-19')
        self.product('101', 'Same name', '1m')
        self.product('102', 'Same name', '1m')
        self.raw('2026-11-18', 3, '0.1', '101')
        self.raw('2026-11-19', 8, '0.2', '101')
        self.raw('2026-11-19', 2, '0.3', '101')
        self.raw('2026-11-18', 6, '0.4', '102')
        self.raw('2026-11-19', 4, '0.5', '103')
        self.raw('2026-11-18', 1, '0.6', '104')
        self.raw('2026-11-19', 1, '0.6', '104')
        result = self.report()
        self.assertEqual(result['base']['revenue'], Decimal('1.6'))
        self.assertEqual(result['previous']['revenue'], Decimal('1.1'))
        self.assertEqual(result['revenue_delta'], Decimal('0.5'))
        self.assertEqual(result['quantity_delta'], 5)
        self.assertEqual(result['quantity_percent'], Decimal(50))
        self.assertEqual(result['counts'], dict(increased=2, decreased=1, unchanged=1))
        self.assertEqual([r['option_id'] for r in result['increases']], ['101', '103'])
        self.assertEqual(result['decreases'][0]['option_id'], '102')
        self.assertEqual(result['decreases'][0]['quantity'], 0)
        self.assertEqual(result['increases'][1]['previous_quantity'], 0)
        self.assertEqual(result['increases'][1]['product_name'], 'Raw name')

    def test_missing_partial_records_never_appear_as_zero_or_ranking(self):
        self.cover('2026-11-18')
        self.raw('2026-11-19', 999)
        result = self.report()
        self.assertFalse(result['comparable'])
        self.assertEqual(result['base']['state'], 'missing')
        self.assertIsNone(result['base']['revenue'])
        self.assertIsNone(result['base']['quantity'])
        self.assertEqual(result['base']['options'], {})
        self.assertEqual(result['rows'], [])
        self.assertEqual(result['increases'], [])
        self.assertIsNone(result['quantity_delta'])
        self.assertEqual(result['previous']['quantity'], 0)

    def test_legacy_day_is_unverified_and_cannot_compare(self):
        self.cover('2026-11-18', legacy=True)
        self.cover('2026-11-19')
        self.raw('2026-11-18', 30)
        result = self.report()
        self.assertEqual(result['previous']['state'], 'unverified')
        self.assertIsNone(result['previous']['quantity'])
        self.assertFalse(result['comparable'])

    def test_completed_zero_days_and_zero_percent_rules(self):
        self.cover('2026-11-18', '2026-11-19')
        result = self.report()
        self.assertTrue(result['comparable'])
        self.assertEqual(result['base']['revenue'], Decimal(0))
        self.assertEqual(result['revenue_percent'], Decimal(0))
        self.raw('2026-11-19', 10, '100')
        result = self.report()
        self.assertIsNone(result['revenue_percent'])
        self.assertIsNone(result['quantity_percent'])
        self.assertEqual(result['revenue_delta'], Decimal(100))

    def test_vendor_isolation_and_external_sales_ignored(self):
        self.cover('2026-11-18', '2026-11-19')
        self.cover('2026-11-17', vendor='OTHER')
        self.raw('2026-11-19', 2, '10')
        self.raw('2026-11-19', 500, '99999', vendor='OTHER')
        self.conn.execute("INSERT INTO sales(sale_date,quantity,revenue,option_id,source) "
                          "VALUES ('2026-11-19',1000,1000000,'101','manual')")
        result = self.report()
        self.assertEqual(result['base']['quantity'], 2)
        self.assertEqual(result['base']['revenue'], Decimal(10))
        self.assertEqual(result['trend'][2]['state'], 'missing')

    def test_catalog_duplicate_latest_rocket_only_no_multiplication(self):
        self.cover('2026-11-18', '2026-11-19')
        self.raw('2026-11-19', 2, '10')
        self.product('101', 'OLD', updated='2026-10-01')
        self.product('101', 'NEW', updated='2026-11-01')
        self.product('101', 'LATEST TIE', updated='2026-11-01')
        self.product('101', 'SELLER', updated='2026-12-01', method='판매자배송')
        self.product('102', 'SAME MODEL DIFFERENT ID')
        result = self.report()
        self.assertEqual(result['base']['quantity'], 2)
        self.assertEqual(len(result['rows']), 1)
        self.assertEqual(result['rows'][0]['product_name'], 'LATEST TIE')

    def test_top_five_stable_ties_and_excludes_unchanged(self):
        self.cover('2026-11-18', '2026-11-19')
        for oid in ('109', '103', '102', '105', '101', '106', '104'):
            self.raw('2026-11-19', 2, oid=oid)
            self.raw('2026-11-18', 2, oid='2' + oid)
        result = self.report()
        self.assertEqual([r['option_id'] for r in result['increases']],
                         ['101', '102', '103', '104', '105'])
        self.assertEqual(len(result['decreases']), 5)
        self.assertEqual(result['counts'], dict(increased=7, decreased=7, unchanged=0))

    def test_filter_and_empty_filter_apply_to_all_days(self):
        self.cover('2026-11-15', '2026-11-18', '2026-11-19')
        for day in ('2026-11-15', '2026-11-18', '2026-11-19'):
            self.raw(day, 2, '10', oid='101')
            self.raw(day, 50, '500', oid='102')
        result = self.report(option_ids=['101', '101'])
        self.assertEqual(result['base']['quantity'], 2)
        self.assertEqual(result['trend'][0]['revenue'], Decimal(10))
        empty = self.report(option_ids=[])
        self.assertEqual(empty['base']['quantity'], 0)
        self.assertEqual(empty['rows'], [])
        self.assertEqual(empty['trend'][1]['state'], 'missing')

    def test_five_day_boundaries_year_and_leap_day(self):
        result = self.report('2026-01-02', '2025-12-31')
        self.assertEqual([d['date'] for d in result['trend']],
            ['2025-12-29', '2025-12-30', '2025-12-31', '2026-01-01', '2026-01-02'])
        result = self.report('2024-03-02', '2024-02-29')
        self.assertEqual([d['date'] for d in result['trend']],
            ['2024-02-27', '2024-02-28', '2024-02-29', '2024-03-01', '2024-03-02'])

    def test_distant_compare_date_does_not_fetch_intervening_dates(self):
        self.cover('2020-01-01', '2026-11-19')
        self.raw('2020-01-01', 4, '20')
        self.raw('2026-06-01', 999, '999999')
        self.raw('2026-11-19', 6, '30')
        with patch.object(read_models, 'daily_api_totals', wraps=read_models.daily_api_totals) as fetch:
            result = self.report(previous='2020-01-01')
        self.assertEqual([(c.args[2], c.args[3]) for c in fetch.call_args_list],
            [('2026-11-15', '2026-11-19'), ('2020-01-01', '2020-01-01')])
        self.assertEqual(result['quantity_delta'], 2)
        self.assertEqual(result['revenue_percent'], Decimal(50))

    def test_dates_reject_equal_reversed_today_future_and_noncanonical(self):
        for base, previous in [('2026-11-19', '2026-11-19'), ('2026-11-18', '2026-11-19'),
                              ('2026-11-20', '2026-11-18'), ('2026-11-21', '2026-11-18'),
                              ('20261119', '2026-11-18'), ('2026-02-30', '2026-02-28'),
                              ('2026-11-19', None)]:
            with self.subTest(base=base, previous=previous), self.assertRaises(ValueError):
                self.report(base, previous)

    def test_query_only_connection_and_caller_transaction_are_preserved(self):
        self.cover('2026-11-18', '2026-11-19')
        self.raw('2026-11-19', 4)
        self.conn.commit()
        before = list(self.conn.iterdump())
        self.conn.execute('PRAGMA query_only=ON')
        self.assertFalse(self.conn.in_transaction)
        self.report()
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(list(self.conn.iterdump()), before)
        self.conn.execute('PRAGMA query_only=OFF')
        self.raw('2026-11-19', 6)
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(self.report()['base']['quantity'], 10)
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertEqual(self.report()['base']['quantity'], 4)


if __name__ == '__main__':
    unittest.main()
