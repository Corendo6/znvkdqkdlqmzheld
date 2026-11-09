"""Inventory decisions use direct Coupang sales, retaining separate order evidence."""
import csv
from datetime import timedelta
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rocket_inventory
import rocket_sales
from test_daily_sales import MemoryDB, NOW, TODAY, VENDOR


class InventorySalesSourceTests(unittest.TestCase):
    def setUp(self):
        self.db = MemoryDB()
        self.conn = self.db.conn
        self.addCleanup(self.conn.close)
        self.app = SimpleNamespace(db=self.db)
        self.conn.executemany('INSERT INTO rg_sales_coverage VALUES (?,?,?)', [
            (VENDOR, (TODAY-timedelta(days=i)).isoformat(), NOW.isoformat()) for i in range(1, 31)])
        self.conn.execute('INSERT INTO rg_order_items VALUES (?,?,?,?,?,?,?,?)',
            (VENDOR, '1', '101', (TODAY-timedelta(days=1)).isoformat(), 'Product', 99, '9900', NOW.isoformat()))
        self.conn.commit()
        self.gross = dict(model='MODEL', estimated_cost=None, cost=None, logistics=None,
                          margin=None, margin_rate=None, missing=[])

    def snapshot(self, quantity, stock=30, pending=0, stamp=None, option='101'):
        self.conn.execute('INSERT OR REPLACE INTO rg_inventory_snapshots VALUES (?,?,?,?,?,?,?)',
            (VENDOR, option, '', stock, quantity, stamp or NOW.isoformat(), 1))
        rocket_inventory.save_pending(self.conn, VENDOR, [option], pending)

    def rows(self, now=NOW):
        with patch.object(rocket_inventory.gross_margin, 'load_context', return_value={}), \
             patch.object(rocket_inventory.gross_margin, 'evaluate', return_value=self.gross):
            return rocket_inventory.build_rows(self.app, now=now)

    def test_direct_30_day_sales_drive_days_and_recommendation(self):
        self.snapshot(300)
        row = self.rows()[0]
        self.assertEqual(row['sales_30'], 300)
        self.assertEqual(row['order_sales_30'], 99)
        self.assertEqual(row['yesterday'], 99)
        self.assertEqual(row['last7'], 99)
        self.assertEqual(row['days'], 3)
        self.assertEqual(row['recommended'], 190)
        self.assertEqual(row['state'], '품절 임박')

    def test_direct_zero_is_not_replaced_by_positive_orders(self):
        self.snapshot(0)
        row = self.rows()[0]
        self.assertEqual(row['sales_30'], 0)
        self.assertEqual(row['order_sales_30'], 99)
        self.assertIsNone(row['recommended'])
        self.assertIsNone(row['days'])

    def test_missing_direct_sales_stays_unknown_even_with_complete_orders(self):
        self.snapshot(None)
        row = self.rows()[0]
        self.assertIsNone(row['sales_30'])
        self.assertEqual(row['order_sales_30'], 99)
        self.assertEqual(row['state'], '판매량 확인')
        self.assertIsNone(row['recommended'])

    def test_direct_sales_does_not_require_order_coverage(self):
        self.conn.execute('DELETE FROM rg_sales_coverage')
        self.conn.commit()
        self.snapshot(300)
        row = self.rows()[0]
        self.assertEqual(row['sales_30'], 300)
        self.assertEqual(row['recommended'], 190)
        self.assertIsNone(row['yesterday'])
        self.assertIsNone(row['last7'])
        self.assertIsNone(row['order_sales_30'])

    def test_stale_inventory_remains_stale_with_complete_daily_orders(self):
        self.snapshot(300)
        row = self.rows(now=NOW+timedelta(hours=25))[0]
        self.assertEqual(row['sales_30'], 300)
        self.assertEqual(row['state'], '재조회 필요')
        self.assertIsNone(row['days'])
        self.assertIsNone(row['recommended'])

    def test_build_does_not_rewrite_order_ledger_or_coverage(self):
        self.snapshot(300)
        before = rocket_sales.daily_quantities(self.conn, VENDOR, TODAY)
        changes = self.conn.total_changes
        self.rows()
        self.assertEqual(self.conn.total_changes, changes)
        self.assertEqual(rocket_sales.daily_quantities(self.conn, VENDOR, TODAY), before)

    def test_multitap_filter_still_applies_to_direct_sales(self):
        self.snapshot(300)
        self.snapshot(600, option='102')
        self.conn.execute("INSERT INTO products(option_id,model,product_name,fulfillment_type) VALUES ('101','MODEL','멀티탭','로켓그로스')")
        self.conn.commit()
        self.app.multitap_only_enabled = lambda: True
        self.app._multitap_product_clause = lambda alias: alias + ".option_id='101'"
        rows = self.rows()
        self.assertEqual([row['option_id'] for row in rows], ['101'])
        self.assertEqual(rows[0]['sales_30'], 300)

    def test_export_distinguishes_direct_and_provisional_sales(self):
        self.snapshot(300)
        rows = self.rows()
        page = SimpleNamespace(rows=rows, selected=set(), filtered_rows=rows)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'inventory.csv'
            with patch.object(rocket_inventory.filedialog, 'asksaveasfilename', return_value=str(path)), \
                 patch.object(rocket_inventory.messagebox, 'showinfo'):
                rocket_inventory.RocketInventoryPage.export_csv(page)
            with path.open(encoding='utf-8-sig', newline='') as stream:
                row = next(csv.DictReader(stream))
        self.assertEqual(row['30일판매수량_쿠팡재고API'], '300')
        self.assertEqual(row['30일판매수량_출고주문_비교용'], '99')
        self.assertEqual(row['어제판매수량_출고주문_잠정'], '99')
        self.assertEqual(row['7일판매수량_출고주문_잠정'], '99')


if __name__ == '__main__':
    unittest.main()
