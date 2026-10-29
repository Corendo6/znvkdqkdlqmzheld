"""Regression checks for sales periods using isolated, in-memory API data."""
import sqlite3
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rocket_sales
import rocket_inventory


TODAY = date(2026, 10, 28)
NOW = datetime(2026, 10, 28, 12, tzinfo=rocket_sales.KST)
VENDOR = 'A00001'


class MemoryDB:
    def __init__(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript('''
            CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE products(id INTEGER PRIMARY KEY, option_id TEXT,
                fulfillment_type TEXT, model TEXT, option_name TEXT, product_name TEXT,
                registered_product_id TEXT, updated_at TEXT);
            CREATE TABLE option_model_map(option_id TEXT PRIMARY KEY, model TEXT);
            CREATE TABLE sales(id INTEGER PRIMARY KEY, sale_date TEXT, model TEXT,
                method TEXT, revenue REAL, orders INTEGER, quantity INTEGER,
                option_id TEXT, option_name TEXT, product_name TEXT,
                registered_product_id TEXT, source TEXT);
            CREATE TABLE sales_pending(sale_date TEXT, option_id TEXT, method TEXT);
            CREATE TABLE unmatched_options(option_id TEXT PRIMARY KEY, option_name TEXT,
                product_name TEXT, registered_product_id TEXT, method TEXT,
                last_seen TEXT);
        ''')
        rocket_sales.ensure_schema(self.conn)
        rocket_inventory.ensure_schema(self.conn)
        self.conn.execute('INSERT INTO settings VALUES (?,?)', ('api_vendor_id', VENDOR))
        self.conn.commit()

    def query(self, sql, args=()):
        return self.conn.execute(sql, args).fetchall()

    def setting(self, name, default=''):
        row = self.conn.execute('SELECT value FROM settings WHERE key=?', (name,)).fetchone()
        return row['value'] if row else default


class SalesTestCase(unittest.TestCase):
    def setUp(self):
        self.db = MemoryDB()
        self.conn = self.db.conn
        self.addCleanup(self.conn.close)
        self.clock = patch.object(rocket_sales, 'today_kst', return_value=TODAY)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def day(self, days_ago):
        return (TODAY - timedelta(days=days_ago)).isoformat()

    def cover(self, days=30, vendor=VENDOR):
        self.conn.executemany('INSERT OR REPLACE INTO rg_sales_coverage VALUES (?,?,?)',
            [(vendor, self.day(offset), NOW.isoformat()) for offset in range(1, days + 1)])
        self.conn.commit()

    def raw(self, days_ago, qty, oid='101', vendor=VENDOR, order_id=None):
        self.conn.execute('INSERT INTO rg_order_items VALUES (?,?,?,?,?,?,?,?)',
            (vendor, order_id or str(10000 + days_ago), oid, self.day(days_ago),
             'Product', qty, str(qty * 50000), NOW.isoformat()))
        self.conn.commit()

    def totals(self, vendor=VENDOR):
        return rocket_sales.daily_quantities(self.conn, vendor, today=TODAY)

    def order(self, days_ago=1, quantity=3):
        return {'order_id': '20001', 'paid_date': self.day(days_ago), 'items': {
            '101': {'option_id': '101', 'quantity': quantity,
                    'revenue': Decimal(quantity * 9000), 'product_name': 'Product'}}}

    def test_exact_window_boundaries_sum_quantity(self):
        self.cover()
        for days, quantity in [(0, 1000), (1, 3), (2, 4), (7, 5), (8, 6), (30, 7), (31, 2000)]:
            self.raw(days, quantity)
        metrics = self.totals()['by_option']['101']
        self.assertEqual(metrics['yesterday'], 3)
        self.assertEqual(metrics['last7'], 12)
        self.assertEqual(metrics['sales_30'], 25)

    def test_vendor_isolation_for_totals_and_coverage(self):
        self.cover(7)
        self.cover(30, vendor='B00002')
        self.raw(1, 4)
        self.raw(1, 500, vendor='B00002')
        result = self.totals()
        self.assertEqual(result['coverage'], {1: 1, 7: 7, 30: 7})
        self.assertEqual(result['by_option']['101']['last7'], 4)
        self.assertIsNone(result['by_option']['101']['sales_30'])

    def test_no_collection_is_unknown(self):
        result = self.totals()
        self.assertEqual(result['coverage'], {1: 0, 7: 0, 30: 0})
        for key in ('yesterday', 'last7', 'sales_30'):
            self.assertIsNone(result['empty'][key])

    def test_complete_zero_sales_is_zero(self):
        self.cover()
        result = self.totals()
        self.assertEqual(result['coverage'], {1: 1, 7: 7, 30: 30})
        for key in ('yesterday', 'last7', 'sales_30'):
            self.assertEqual(result['empty'][key], 0)

    def test_incomplete_windows_do_not_present_partial_sum_as_total(self):
        self.cover(1)
        self.raw(1, 8)
        metrics = self.totals()['by_option']['101']
        self.assertEqual(metrics['yesterday'], 8)
        self.assertIsNone(metrics['last7'])
        self.assertIsNone(metrics['sales_30'])

    def test_manual_and_excel_rows_do_not_contaminate_api_totals(self):
        self.cover()
        self.raw(1, 3)
        for source in ('manual', 'coupang-insights', 'rg-order-api:B00002'):
            self.conn.execute('INSERT INTO sales(sale_date,method,quantity,option_id,source) VALUES (?,?,?,?,?)',
                (self.day(1), '로켓그로스', 900, '101', source))
        self.assertEqual(self.totals()['by_option']['101']['sales_30'], 3)

    def test_successful_empty_fetch_covers_every_requested_day(self):
        rocket_sales.save_orders(self.conn, VENDOR, [], self.day(30), self.day(1))
        self.assertEqual(self.totals()['coverage'], {1: 1, 7: 7, 30: 30})
        self.assertEqual(self.totals()['empty']['sales_30'], 0)

    def test_refetch_is_idempotent_and_updated_quantity_replaces_old(self):
        order = self.order()
        for _ in range(2):
            rocket_sales.save_orders(self.conn, VENDOR, [order], self.day(30), self.day(1))
        self.assertEqual(self.totals()['by_option']['101']['sales_30'], 3)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM rg_order_items').fetchone()[0], 1)
        rocket_sales.save_orders(self.conn, VENDOR, [self.order(quantity=5)], self.day(7), self.day(1))
        self.assertEqual(self.totals()['by_option']['101']['sales_30'], 5)

    def test_coverage_and_orders_roll_back_together(self):
        self.conn.execute('''CREATE TRIGGER reject_coverage BEFORE INSERT ON rg_sales_coverage
            WHEN NEW.sale_date='2026-10-26'
            BEGIN SELECT RAISE(ABORT, 'test coverage write failure'); END''')
        self.conn.commit()
        with self.assertRaises(sqlite3.DatabaseError):
            rocket_sales.save_orders(self.conn, VENDOR, [self.order()], self.day(3), self.day(1))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM rg_sales_coverage').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM rg_order_items').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM sales').fetchone()[0], 0)

    def test_failed_second_page_does_not_modify_existing_data(self):
        self.cover(7)
        self.raw(1, 2)
        before = self.totals()
        replies = iter([{'code': 200, 'data': [], 'nextToken': 'page-two'},
                        *[{'code': 500, 'message': 'failed'}] * 3])
        api = SimpleNamespace(vendor_id=VENDOR, check_cancelled=lambda: None,
                              _request=lambda *args: next(replies), wait=lambda seconds: None)
        with self.assertRaises(ValueError):
            orders = rocket_sales.fetch_orders(api, self.day(30), self.day(1))
            rocket_sales.save_orders(self.conn, VENDOR, orders, self.day(30), self.day(1))
        self.assertEqual(self.totals(), before)

    def test_epoch_milliseconds_use_korean_payment_day(self):
        stamp = datetime(2026, 10, 26, 16, tzinfo=timezone.utc).timestamp() * 1000
        raw = {'vendorId': VENDOR, 'orderId': '1001', 'paidAt': int(stamp), 'orderItems': [
            {'currency': 'KRW', 'vendorItemId': '101', 'salesQuantity': 3,
             'unitSalesPrice': 9999, 'productName': 'Product'}]}
        order = rocket_sales.normalize_order(raw, VENDOR, self.day(1), self.day(1))
        self.assertEqual(order['paid_date'], '2026-10-27')
        self.assertEqual(order['items']['101']['quantity'], 3)

    def test_valid_legacy_successful_window_is_migrated(self):
        self.conn.execute('DROP TABLE rg_sales_coverage')
        for key, value in [('last_sync', '2025-01-31T12:00:00+09:00'),
                           ('last_from', '2025-01-01'), ('last_to', '2025-01-30')]:
            self.conn.execute('INSERT INTO settings VALUES (?,?)', ('rg_orders_' + key + ':' + VENDOR, value))
        self.conn.commit()
        rocket_sales.ensure_schema(self.conn)
        rows = self.conn.execute('SELECT sale_date FROM rg_sales_coverage WHERE vendor_id=? ORDER BY sale_date',
                                 (VENDOR,)).fetchall()
        self.assertEqual(len(rows), 30)
        self.assertEqual(rows[0]['sale_date'], '2025-01-01')
        self.assertEqual(rows[-1]['sale_date'], '2025-01-30')

    def test_invalid_legacy_window_does_not_create_coverage(self):
        self.conn.execute('DROP TABLE rg_sales_coverage')
        for key, value in [('last_sync', 'not-a-timestamp'),
                           ('last_from', '2025-01-01'), ('last_to', '2025-01-30')]:
            self.conn.execute('INSERT INTO settings VALUES (?,?)', ('rg_orders_' + key + ':' + VENDOR, value))
        self.conn.commit()
        rocket_sales.ensure_schema(self.conn)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM rg_sales_coverage').fetchone()[0], 0)

    def inventory(self, stock=0, inventory_sales=0):
        self.conn.execute('INSERT INTO rg_inventory_snapshots VALUES (?,?,?,?,?,?,?)',
            (VENDOR, '101', '', stock, inventory_sales, NOW.isoformat(), 1))
        gross = dict(model='MODEL', estimated_cost=None, cost=None, logistics=None,
                     margin=None, margin_rate=None, missing=[])
        with patch.object(rocket_inventory.gross_margin, 'load_context', return_value={}), \
             patch.object(rocket_inventory.gross_margin, 'evaluate', return_value=gross):
            return rocket_inventory.build_rows(SimpleNamespace(db=self.db), now=NOW)

    def test_sold_out_option_with_daily_sales_remains_visible(self):
        self.cover()
        self.raw(1, 4)
        rows = self.inventory(stock=0, inventory_sales=0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['sales_30'], 4)

    def test_inventory_30day_summary_is_not_used_as_sales_source(self):
        self.cover()
        self.raw(1, 4)
        rows = self.inventory(stock=20, inventory_sales=999)
        self.assertEqual(rows[0]['sales_30'], 4)
        self.assertEqual(rows[0]['days'], 150)

    def test_unknown_sales_does_not_hide_zero_stock_option(self):
        rows = self.inventory(stock=0, inventory_sales=0)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]['sales_30'])
        self.assertIsNone(rows[0]['recommended'])

    def test_known_zero_sales_hides_zero_stock_option(self):
        self.cover()
        self.assertEqual(self.inventory(stock=0, inventory_sales=999), [])


if __name__ == '__main__':
    unittest.main()
