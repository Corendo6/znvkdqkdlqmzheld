"""Shared projections keep cache revisions, raw API totals, and precedence intact."""
import sqlite3
import sys
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gross_margin
import read_models
import rocket_sales
from test_daily_sales import MemoryDB, TODAY, VENDOR


class ReadModelTests(unittest.TestCase):
    def setUp(self):
        self.db = MemoryDB()
        self.conn = self.db.conn
        self.addCleanup(self.conn.close)
        self.addCleanup(read_models.clear, self.conn)
        self.first = (TODAY - timedelta(days=30)).isoformat()
        self.last = (TODAY - timedelta(days=1)).isoformat()

    def product(self, oid='101', model='PRODUCT', updated='2026-10-27', method='로켓그로스'):
        self.conn.execute('''INSERT INTO products(option_id,fulfillment_type,model,
            option_name,product_name,registered_product_id,updated_at) VALUES (?,?,?,?,?,?,?)''',
            (oid, method, model, 'Option', model + ' name', '999', updated))

    def raw(self, oid='101', vendor=VENDOR, day=None, order='1', qty=2):
        self.conn.execute('INSERT INTO rg_order_items VALUES (?,?,?,?,?,?,?,?)',
                          (vendor, order, oid, day or self.last, 'Raw name', qty, '0.1', self.last))

    def pricing_schema(self):
        self.conn.executescript('''
            CREATE TABLE gross_margin_profiles(model TEXT PRIMARY KEY, manual_cost REAL,
                cost_source TEXT, size_type TEXT, source TEXT);
            CREATE TABLE margin_refs(id INTEGER PRIMARY KEY,model TEXT,method TEXT,
                size_type TEXT,sale_price REAL,cost REAL,inout_fee REAL,shipping_fee REAL,
                size_desc TEXT,weight REAL);
            CREATE TABLE fulfillment_rates(id INTEGER PRIMARY KEY,size_type TEXT,
                min_price REAL,max_price REAL,inout_current REAL,inout_regular REAL,
                shipping_current REAL,shipping_regular REAL);
            INSERT INTO gross_margin_profiles VALUES ('PRODUCT',1000,'manual','극소형','manual');
            INSERT INTO gross_margin_profiles VALUES ('MAPPED',9000,'manual','극소형','manual');
            INSERT INTO margin_refs VALUES (1,'PRODUCT','로켓그로스','극소형',10000,1000,10,20,'',NULL);
            INSERT INTO fulfillment_rates VALUES (1,'극소형',0,NULL,10,10,20,20);
        ''')

    def test_catalog_reuses_immutable_snapshot_and_invalidates_own_committed_change(self):
        self.product(model='OLD', updated='2026-10-26')
        self.product(model='NEW')
        self.product(model='TIE')
        self.product(model='SELLER', updated='2026-12-01', method='판매자배송')
        self.conn.commit()
        first = read_models.catalog_snapshot(self.conn)
        self.assertEqual(first['products']['101']['model'], 'TIE')
        statements = []
        self.conn.set_trace_callback(statements.append)
        second = read_models.catalog_snapshot(self.conn)
        self.conn.set_trace_callback(None)
        self.assertIs(first, second)
        self.assertFalse(any(sql.startswith('SELECT') for sql in statements))
        with self.assertRaises(TypeError):
            second['products']['101']['model'] = 'corrupted'
        self.conn.execute("UPDATE products SET model='EDITED' WHERE model='TIE'")
        self.conn.commit()
        self.assertEqual(read_models.catalog_snapshot(self.conn)['products']['101']['model'], 'EDITED')

    def test_active_transaction_reads_are_not_reused_after_rollback(self):
        self.product()
        self.conn.commit()
        self.assertEqual(read_models.catalog_snapshot(self.conn)['products']['101']['model'], 'PRODUCT')
        self.conn.execute("UPDATE products SET model='UNCOMMITTED'")
        self.assertEqual(read_models.catalog_snapshot(self.conn)['products']['101']['model'], 'UNCOMMITTED')
        self.conn.rollback()
        self.assertEqual(read_models.catalog_snapshot(self.conn)['products']['101']['model'], 'PRODUCT')

    def test_other_connection_commit_invalidates_snapshot_without_local_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'revision.db')
            left, right = sqlite3.connect(path), sqlite3.connect(path)
            try:
                left.execute('CREATE TABLE sample(value INTEGER)')
                left.execute('INSERT INTO sample VALUES (1)')
                left.commit()
                loader = lambda: left.execute('SELECT value FROM sample').fetchone()[0]
                self.assertEqual(read_models.cached_read(left, 'value', loader), 1)
                changes = left.total_changes
                right.execute('UPDATE sample SET value=2')
                right.commit()
                self.assertEqual(left.total_changes, changes)
                self.assertEqual(read_models.cached_read(left, 'value', loader), 2)
            finally:
                read_models.clear(left)
                left.close()
                right.close()

    def test_schema_change_invalidates_and_failed_factory_is_not_cached(self):
        loader = lambda: self.conn.execute('SELECT COUNT(*) FROM sqlite_master WHERE name=?', ('later',)).fetchone()[0]
        self.assertEqual(read_models.cached_read(self.conn, 'schema', loader), 0)
        before = self.conn.total_changes
        self.conn.execute('CREATE TABLE later(value TEXT)')
        self.assertEqual(self.conn.total_changes, before)
        self.assertEqual(read_models.cached_read(self.conn, 'schema', loader), 1)
        fail_once = Mock(side_effect=[ValueError('read failed'), {'ready': True}])
        with self.assertRaises(ValueError):
            read_models.cached_read(self.conn, 'failure', fail_once)
        self.assertEqual(read_models.cached_read(self.conn, 'failure', fail_once), {'ready': True})
        self.assertEqual(fail_once.call_count, 2)

    def test_commit_during_factory_does_not_retain_mixed_revision(self):
        factory = Mock()
        def load():
            self.conn.execute("INSERT OR REPLACE INTO settings VALUES ('edited','yes')")
            self.conn.commit()
            return 'first'
        factory.side_effect = load
        self.assertEqual(read_models.cached_read(self.conn, 'mixed', factory), 'first')
        factory.side_effect = None
        factory.return_value = 'second'
        self.assertEqual(read_models.cached_read(self.conn, 'mixed', factory), 'second')
        self.assertEqual(factory.call_count, 2)

    def test_raw_totals_preserve_decimal_vendor_option_and_date_scope(self):
        for number in range(10):
            self.raw(order=str(number))
        self.raw(oid='102')
        self.raw(vendor='OTHER', qty=999)
        self.raw(day=self.first, order='old', qty=8)
        self.conn.commit()
        latest = read_models.daily_api_totals(self.conn, VENDOR, self.last, self.last, ['101'])
        self.assertEqual(set(latest), {(self.last, '101')})
        self.assertEqual(latest[(self.last, '101')]['revenue'], Decimal('1.0'))
        self.assertEqual(latest[(self.last, '101')]['orders'], 10)
        self.assertEqual(latest[(self.last, '101')]['quantity'], 20)
        all_days = read_models.daily_api_totals(self.conn, VENDOR, self.first, self.last)
        self.assertEqual(len(all_days), 3)
        self.assertEqual(read_models.daily_api_totals(self.conn, VENDOR, option_ids=[]), {})

    def test_daily_windows_recompute_at_midnight_and_refetch_updates_cached_totals(self):
        with patch.object(rocket_sales, 'today_kst', return_value=TODAY):
            order = dict(order_id='1', paid_date=self.last, items={
                '101': dict(product_name='Raw', quantity=2, revenue=Decimal('0.1'))})
            rocket_sales.save_orders(self.conn, VENDOR, [order], self.first, self.last)
            self.assertEqual(rocket_sales.daily_quantities(self.conn, VENDOR, TODAY)['by_option']['101']['yesterday'], 2)
            next_day = rocket_sales.daily_quantities(self.conn, VENDOR, TODAY + timedelta(days=1))
            self.assertIsNone(next_day['by_option']['101']['yesterday'])
            order['items']['101']['quantity'] = 5
            rocket_sales.save_orders(self.conn, VENDOR, [order], self.first, self.last)
            self.assertEqual(rocket_sales.daily_quantities(self.conn, VENDOR, TODAY)['by_option']['101']['yesterday'], 5)

    def test_readonly_status_matches_rebuild_precedence_without_any_writes(self):
        for option in ('101', '102', '103', '104'):
            self.raw(oid=option)
        self.product('101', 'PRODUCT')
        self.conn.execute("INSERT INTO option_model_map VALUES ('101','MAPPED')")
        self.conn.execute("INSERT INTO sales(sale_date,model,method,source) VALUES (?,'MAPPED','로켓그로스','manual')", (self.last,))
        self.conn.execute("INSERT INTO sales_pending VALUES (?,'102','로켓그로스')", (self.last,))
        self.product('104', 'LINKED')
        self.conn.commit()
        before = self.conn.total_changes
        status = rocket_sales.sales_status(self.conn, VENDOR, self.first, self.last)
        self.assertEqual(self.conn.total_changes, before)
        self.assertEqual(status, dict(conflicts=2, unmapped=1))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM unmatched_options').fetchone()[0], 0)
        result = rocket_sales.rebuild_sales(self.conn, VENDOR, self.first, self.last)
        self.assertEqual({key: result[key] for key in status}, status)

    def test_margin_keeps_product_model_priority_and_price_fallback_explicit(self):
        self.pricing_schema()
        self.conn.execute("INSERT INTO option_model_map VALUES ('101','MAPPED')")
        self.conn.commit()
        context = gross_margin.load_context(self.db)
        product = dict(option_id='101', model='PRODUCT', sale_price=None)
        fallback = gross_margin.evaluate(product, context)
        strict = gross_margin.evaluate(product, context, allow_reference_price=False)
        self.assertEqual(fallback['model'], 'PRODUCT')
        self.assertEqual(fallback['cost'], 1000)
        self.assertEqual(fallback['sale'], 10000)
        self.assertIsNone(strict['sale'])
        self.assertIsNone(strict['margin'])
        context['sizes']['PRODUCT'] = ('특대형', 'temporary')
        fresh = gross_margin.load_context(self.db)
        self.assertEqual(fresh['sizes'], {})
        self.conn.execute("UPDATE gross_margin_profiles SET manual_cost=2000 WHERE model='PRODUCT'")
        self.conn.execute("INSERT INTO settings VALUES ('commission_rate','9.9')")
        self.conn.commit()
        updated = gross_margin.evaluate(product, gross_margin.load_context(self.db))
        self.assertEqual(updated['cost'], 2000)
        self.assertEqual(updated['commission'], 9.9)


if __name__ == '__main__':
    unittest.main()
