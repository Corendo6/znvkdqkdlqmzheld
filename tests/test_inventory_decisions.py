"""Inventory views/export share decisions, with conservative unknown-state handling."""
import csv
from datetime import timedelta
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rocket_inventory
from test_daily_sales import MemoryDB, NOW, VENDOR


class InventoryDecisionTests(unittest.TestCase):
    def metrics(self, **changes):
        args = dict(stock=20, sales_30=90, pending=10, fetched_at=NOW.isoformat(),
                    last7=21, now=NOW)
        args.update(changes)
        return rocket_inventory.decision_metrics(**args)

    def test_shortage_and_recommendation_keep_distinct_formulas(self):
        result = self.metrics(pack=12)
        self.assertEqual(result['gap7'], 22.5)
        self.assertEqual(result['recommended'], 36)
        self.assertEqual(result['state'], '품절 임박')
        self.assertAlmostEqual(result['days'], 20 / 3)

    def test_surplus_remains_negative_and_unknown_pending_stays_unknown(self):
        self.assertEqual(self.metrics(stock=100)['gap7'], -57.5)
        result = self.metrics(pending=None)
        self.assertIsNone(result['gap7'])
        self.assertIsNone(result['recommended'])

    def test_seven_day_known_does_not_invent_thirty_day_coverage(self):
        result = self.metrics(sales_30=None)
        self.assertEqual(result['gap7'], 22.5)
        self.assertIsNone(result['recommended'])
        self.assertIsNone(result['days'])

    def test_time_boundary_invalidates_decisions_without_a_database_write(self):
        self.assertIsNotNone(self.metrics(now=NOW + timedelta(hours=24))['gap7'])
        result = self.metrics(now=NOW + timedelta(hours=24, seconds=1))
        self.assertEqual(result['state'], '재조회 필요')
        self.assertIsNone(result['gap7'])
        self.assertIsNone(result['recommended'])

    def test_missing_snapshot_and_missing_quantity_are_not_zero(self):
        for changes in ({'present': False}, {'stock': None}, {'fetched_at': None}):
            with self.subTest(changes=changes):
                result = self.metrics(**changes)
                self.assertIsNone(result['gap7'])
                self.assertIsNone(result['recommended'])

    def test_export_uses_the_same_shortage_as_the_display_row(self):
        row = dict(key='101', product_name='Test', **self.metrics(stock=100))
        page = SimpleNamespace(rows=[row], selected=set(), filtered_rows=[row])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'inventory.csv'
            with patch.object(rocket_inventory.filedialog, 'asksaveasfilename', return_value=str(path)), \
                 patch.object(rocket_inventory.messagebox, 'showinfo'):
                rocket_inventory.RocketInventoryPage.export_csv(page)
            with path.open(encoding='utf-8-sig', newline='') as stream:
                saved = next(csv.DictReader(stream))
            self.assertEqual(float(saved['부족분_7일판매x2.5_재고입고중차감']), row['gap7'])


class InventoryRequestTests(unittest.TestCase):
    def api(self, responses):
        return SimpleNamespace(vendor_id=VENDOR, check_cancelled=Mock(), wait=Mock(),
                               _request=Mock(side_effect=responses), progress=Mock())

    def row(self, option='101', stock=5):
        return dict(vendorId=VENDOR, vendorItemId=option,
                    inventoryDetails=dict(totalOrderableQuantity=stock))

    def test_transient_json_retry_keeps_inventory_page_token(self):
        api = self.api([dict(code=200, data=[self.row()], nextToken='opaque/+=='),
                        dict(code=429), dict(code=200, data=[self.row('102')])])
        rows = rocket_inventory.fetch_inventory(api)
        self.assertEqual({row['option_id'] for row in rows}, {'101', '102'})
        self.assertEqual(api._request.call_args_list[1], api._request.call_args_list[2])
        self.assertEqual(api._request.call_args_list[1].args[2], [('nextToken', 'opaque/+==')])
        api.wait.assert_called_once_with(30)

    def test_bad_vendor_is_not_retried_and_saved_snapshot_is_preserved(self):
        db = MemoryDB()
        self.addCleanup(db.conn.close)
        rocket_inventory.save_snapshot(db.conn, VENDOR, [dict(option_id='old', orderable_quantity=8)])
        before = [tuple(row) for row in db.conn.execute('SELECT * FROM rg_inventory_snapshots')]
        api = self.api([dict(code=200, data=[dict(self.row(), vendorId='OTHER')])])
        with self.assertRaises(ValueError):
            rows = rocket_inventory.fetch_inventory(api)
            rocket_inventory.save_snapshot(db.conn, VENDOR, rows)
        self.assertEqual(api._request.call_count, 1)
        api.wait.assert_not_called()
        self.assertEqual(before, [tuple(row) for row in db.conn.execute('SELECT * FROM rg_inventory_snapshots')])


if __name__ == '__main__':
    unittest.main()
