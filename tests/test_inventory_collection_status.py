"""User-visible sales distinguish a collected zero from incomplete date coverage."""
import csv
from datetime import timedelta
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rocket_inventory
from test_daily_sales import MemoryDB, NOW, TODAY, VENDOR


class InventoryCollectionStatusTests(unittest.TestCase):
    def setUp(self):
        self.db = MemoryDB()
        self.addCleanup(self.db.conn.close)
        self.app = SimpleNamespace(db=self.db)
        self.db.conn.execute('INSERT INTO rg_inventory_snapshots VALUES (?,?,?,?,?,?,?)',
            (VENDOR, '101', '', 30, None, NOW.isoformat(), 1))
        self.db.conn.commit()

    def cover(self, offsets):
        self.db.conn.executemany('INSERT INTO rg_sales_coverage VALUES (?,?,?)', [
            (VENDOR, (TODAY-timedelta(days=n)).isoformat(), NOW.isoformat()) for n in offsets])
        self.db.conn.commit()

    def row(self):
        gross = dict(model='MODEL', estimated_cost=None, cost=None, logistics=None,
                     margin=None, margin_rate=None, missing=[], sale=None)
        with patch.object(rocket_inventory.gross_margin, 'load_context', return_value={}), \
             patch.object(rocket_inventory.gross_margin, 'evaluate', return_value=gross):
            return rocket_inventory.build_rows(self.app, NOW)[0]

    def test_successful_empty_yesterday_remains_zero_and_provisional(self):
        self.cover([1])
        row = self.row()
        self.assertEqual(row['yesterday'], 0)
        self.assertEqual(row['yesterday_status'], '잠정')
        self.assertEqual(row['yesterday_fetched_at'], NOW.isoformat())
        self.assertEqual(row['last7_status'], '미수집')
        self.assertEqual(row['sales_30_status'], '미수집')
        self.assertEqual(row['sales_30_source'], '미수집')
        self.assertIn((TODAY-timedelta(days=2)).isoformat(), row['sales_missing_dates'])
        self.assertNotIn((TODAY-timedelta(days=1)).isoformat(), row['sales_missing_dates'])

    def test_sheet_shows_missing_sales_but_keeps_stock_unknown_as_dash(self):
        row = self.row()
        row['stock'] = None
        row['yesterday'] = 0
        sheet = Mock()
        sheet.get_children.return_value = ()
        page = SimpleNamespace(sheet=sheet, view_mode=SimpleNamespace(get=lambda: '엑셀형 보기'),
            visible_rows=[row], selected=set(),
            sheet_columns=[(key, key, 100) for key in ('yesterday', 'last7', 'sales_30', 'stock')])
        rocket_inventory.RocketInventoryPage._render_sheet(page)
        self.assertEqual(sheet.insert.call_args.kwargs['values'], ['0', '미수집', '미수집', '—'])

    def test_legacy_date_requires_recheck_instead_of_claiming_zero(self):
        yesterday = (TODAY-timedelta(days=1)).isoformat()
        self.db.conn.execute('INSERT INTO rg_sales_legacy_coverage VALUES (?,?,?)',
                             (VENDOR, yesterday, NOW.isoformat()))
        self.db.conn.commit()
        row = self.row()
        self.assertIsNone(row['yesterday'])
        self.assertEqual(row['yesterday_status'], '재확인 필요')
        self.assertEqual(row['last7_status'], '재확인 필요')
        self.assertEqual(rocket_inventory.format_sales(row['yesterday'], row['yesterday_status']), '재확인 필요')

    def test_export_preserves_numeric_empty_and_adds_explicit_status(self):
        self.cover([1])
        row = self.row()
        page = SimpleNamespace(rows=[row], selected=set(), filtered_rows=[row])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'status.csv'
            with patch.object(rocket_inventory.filedialog, 'asksaveasfilename', return_value=str(path)), \
                 patch.object(rocket_inventory.messagebox, 'showinfo'):
                rocket_inventory.RocketInventoryPage.export_csv(page)
            with path.open(encoding='utf-8-sig', newline='') as stream:
                saved = next(csv.DictReader(stream))
        self.assertEqual(saved['어제판매수량_출고주문_잠정'], '0')
        self.assertEqual(saved['7일판매수량_출고주문_잠정'], '')
        self.assertEqual(saved['어제판매량_상태'], '잠정')
        self.assertEqual(saved['7일판매량_상태'], '미수집')
        self.assertEqual(saved['30일판매량_상태'], '미수집')
        self.assertEqual(saved['어제판매량_수집시각'], NOW.isoformat())
        self.assertEqual(saved['출고주문_최근30일수집필요날짜'], row['sales_missing_dates'])


if __name__ == '__main__':
    unittest.main()
