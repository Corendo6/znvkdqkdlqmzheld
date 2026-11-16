"""Sync orchestration regressions: isolated SQLite stores, no GUI or network."""
import ast
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
import queue
import sys
import threading
import time
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_daily_sales import MemoryDB, TODAY, VENDOR
import rocket_inventory
import rocket_sales


class Cancelled(Exception):
    stop_sync = True


class InlineThread:
    def __init__(self, target, **kwargs):
        self.target = target

    def start(self):
        self.target()


class RecordingQueue(queue.Queue):
    def __init__(self, conn):
        super().__init__()
        self.conn = conn
        self.saved_transactions = []
        self.after_inventory = None

    def put(self, item):
        if item[0] in ('inventory_saved', 'orders_saved'):
            self.saved_transactions.append((item[0], self.conn.in_transaction))
        super().put(item)
        if item[0] == 'inventory_saved' and self.after_inventory:
            self.after_inventory()


class SyncQueryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.db = MemoryDB()
        self.addCleanup(self.db.conn.close)
        self.db.close = Mock()
        def set_setting(key, value):
            with self.db.conn:
                self.db.conn.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, value))
        self.db.set_setting = set_setting
        self.first = (TODAY - timedelta(days=30)).isoformat()
        self.last = (TODAY - timedelta(days=1)).isoformat()
        self.clock = patch.object(rocket_sales, 'today_kst', return_value=TODAY)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.api = SimpleNamespace(vendor_id=VENDOR, progress=None, phase='', cancel_event=None)
        def check_cancelled():
            if self.api.cancel_event.is_set():
                raise Cancelled()
        self.api.check_cancelled = check_cancelled
        self.events = RecordingQueue(self.db.conn)
        self.app = SimpleNamespace(api_busy=False, db=self.db,
            save_api_settings=Mock(return_value=True),
            _credentials_from_db=Mock(return_value=(VENDOR, 'fake-access', 'fake-secret')),
            api_scope=SimpleNamespace(get=lambda: '전체 갱신'),
            api_sales_from=SimpleNamespace(get=lambda: self.first),
            api_sales_to=SimpleNamespace(get=lambda: self.last),
            _set_api_status=Mock(), _update_api_controls=Mock(), api_queue=self.events,
            _sync_product_data=Mock(return_value={'ok': True, 'text': 'products saved'}))
        source = Path(__file__).resolve().parents[1] / 'coupang_manager.pyw'
        tree = ast.parse(source.read_text(encoding='utf-8-sig'))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'CoupangManager')
        method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'api_sync_all')
        self.namespace = dict(rocket_sales=rocket_sales, rocket_inventory=rocket_inventory,
            CoupangAPI=Mock(return_value=self.api), Database=Mock(return_value=self.db), DB_PATH='unused',
            CoupangAPICancelled=Cancelled, LOGGER=Mock(), APP_NAME='test', messagebox=Mock(),
            time=time, threading=SimpleNamespace(Event=threading.Event, Thread=InlineThread))
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), 'exec'), self.namespace)
        self.app.api_sync_all = MethodType(self.namespace['api_sync_all'], self.app)
        self.order = dict(order_id='20001', paid_date=self.last, items={
            '101': dict(option_id='101', quantity=3, revenue=Decimal('27000'), product_name='Product')})
        self.inventory = [dict(option_id='101', external_sku_id='sku', orderable_quantity=17, sales_30=999)]
        self.fetch_inventory = patch.object(rocket_inventory, 'fetch_inventory', return_value=self.inventory)
        self.inv_mock = self.fetch_inventory.start()
        self.addCleanup(self.fetch_inventory.stop)
        self.fetch_orders = patch.object(rocket_sales, 'fetch_orders',
            side_effect=lambda api, start, end, *args: [self.order] if start <= self.last <= end else [])
        self.orders_mock = self.fetch_orders.start()
        self.addCleanup(self.fetch_orders.stop)

    def run_sync(self, scope='전체 갱신', force=False):
        self.app.api_sync_all(scope=scope, sales_force=force)
        events = []
        while not self.events.empty():
            events.append(self.events.get_nowait())
        self.assertEqual(events[-1][0], 'sync_complete')
        self.db.close.assert_called_once()
        return events, events[-1][1]

    def test_order_failure_preserves_committed_stock_and_prior_sales(self):
        rocket_sales.save_orders(self.db.conn, VENDOR, [self.order], self.first, self.last)
        self.orders_mock.side_effect = ValueError('failed last order page')
        events, results = self.run_sync(force=True)
        self.assertTrue(results['inventory']['ok'])
        self.assertFalse(results['orders']['ok'])
        self.assertTrue(results['products']['ok'])
        self.assertEqual(self.db.conn.execute('SELECT orderable_quantity FROM rg_inventory_snapshots').fetchone()[0], 17)
        self.assertEqual(self.db.conn.execute('SELECT quantity FROM rg_order_items').fetchone()[0], 3)
        self.assertEqual(self.events.saved_transactions[0], ('inventory_saved', False))
        self.assertTrue(all(not active for _, active in self.events.saved_transactions))
        self.assertEqual(self.db.conn.execute('SELECT COUNT(*) FROM rg_sales_coverage').fetchone()[0], 30)
        self.assertIn('failed last order page', self.db.setting('rg_orders_last_error:'+VENDOR))

    def test_product_failure_preserves_committed_orders_and_coverage(self):
        self.app._sync_product_data.side_effect = ValueError('product failed')
        events, results = self.run_sync()
        self.assertTrue(results['orders']['ok'])
        self.assertFalse(results['products']['ok'])
        self.assertEqual(self.db.conn.execute('SELECT COUNT(*) FROM rg_sales_coverage').fetchone()[0], 30)
        self.assertEqual(self.db.conn.execute('SELECT quantity FROM sales').fetchone()[0], 3)
        self.assertEqual(self.events.saved_transactions[0], ('inventory_saved', False))
        self.assertGreaterEqual(sum(kind == 'orders_saved' for kind, _ in self.events.saved_transactions), 30)
        self.assertTrue(all(not active for _, active in self.events.saved_transactions))
        self.assertLess([e[0] for e in events].index('orders_saved'), [e[0] for e in events].index('sync_complete'))

    def test_cancel_after_stock_save_keeps_stock_and_does_not_start_orders(self):
        self.events.after_inventory = lambda: self.app.api_cancel_event.set()
        events, results = self.run_sync()
        self.assertTrue(results['inventory']['ok'])
        self.assertTrue(events[-1][2])
        self.orders_mock.assert_not_called()
        self.app._sync_product_data.assert_not_called()
        self.assertEqual(self.db.conn.execute('SELECT orderable_quantity FROM rg_inventory_snapshots').fetchone()[0], 17)
        self.assertEqual(self.events.saved_transactions, [('inventory_saved', False)])

    def test_products_relink_once_then_summary_is_read_only(self):
        def link_products(api, db):
            with db.conn:
                db.conn.execute('INSERT INTO option_model_map VALUES (?,?)', ('101', 'MODEL'))
            return {'ok': True, 'text': 'products saved'}
        self.app._sync_product_data.side_effect = link_products
        readonly_changes = []
        status = rocket_sales.sales_status
        def observe_status(*args, **kwargs):
            before = self.db.conn.total_changes
            result = status(*args, **kwargs)
            readonly_changes.append(self.db.conn.total_changes - before)
            return result
        with patch.object(rocket_sales, 'rebuild_sales', wraps=rocket_sales.rebuild_sales) as rebuild, \
             patch.object(rocket_sales, 'sales_status', side_effect=observe_status) as summary:
            _, results = self.run_sync()
        # Each complete date rebuilds its totals; one final pass links the newly fetched products.
        self.assertEqual(rebuild.call_count, 31)
        daily_ranges = [call.args[2:] for call in rebuild.call_args_list[:-1]]
        self.assertEqual({first for first, last in daily_ranges},
                         {(TODAY-timedelta(days=i)).isoformat() for i in range(1, 31)})
        self.assertTrue(all(first == last for first, last in daily_ranges))
        self.assertEqual(rebuild.call_args_list[-1].args, (self.db.conn, VENDOR))
        summary.assert_called_once_with(self.db.conn, VENDOR, self.first, self.last)
        self.assertEqual(readonly_changes, [0])
        self.assertEqual(self.db.conn.execute('SELECT model FROM sales').fetchone()[0], 'MODEL')
        self.assertIn('모델 미연결 0', results['orders']['text'])

    def test_products_only_relinks_saved_history_without_fetching_orders(self):
        rocket_sales.save_orders(self.db.conn, VENDOR, [self.order], self.first, self.last)
        with patch.object(rocket_sales, 'rebuild_sales', wraps=rocket_sales.rebuild_sales) as rebuild:
            _, results = self.run_sync('상품정보·판매상태만')
        self.orders_mock.assert_not_called()
        self.inv_mock.assert_not_called()
        self.assertEqual(rebuild.call_count, 1)
        self.assertEqual(set(results), {'products'})


class ProductDetailBoundaryTests(unittest.TestCase):
    def setUp(self):
        source = Path(__file__).resolve().parents[1] / 'coupang_manager.pyw'
        tree = ast.parse(source.read_text(encoding='utf-8-sig'))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'CoupangManager')
        method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '_sync_product_data')
        namespace = dict(LOGGER=Mock(), datetime=datetime)
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), 'exec'), namespace)
        self.db = SimpleNamespace(setting=lambda key, default='': default, set_setting=Mock())
        self.app = SimpleNamespace(api_queue=queue.Queue(),
            _merge_api_products=Mock(return_value=(0, 0, [])),
            _merge_failed_list_products=Mock(return_value=(0, 1)),
            _auto_repair_model_links=Mock(return_value={}),
            _refresh_sale_status_from_api=Mock(return_value={'errors': 0}))
        self.app._sync_product_data = MethodType(namespace['_sync_product_data'], self.app)
        self.api = SimpleNamespace(vendor_id=VENDOR, wait=Mock(), check_cancelled=Mock(),
            get_all_products=Mock(side_effect=[
                [dict(sellerProductId='501', productId='601', items=[{'vendorItemId': '101'}])],
                [dict(sellerProductId='501', productId='602')]]),
            get_product=Mock(return_value={'sellerProductId': '501'}))

    def test_list_overlap_queries_one_detail_and_preserves_both_exposure_ids(self):
        result = self.app._sync_product_data(self.api, self.db)
        self.assertTrue(result['ok'])
        self.api.get_product.assert_called_once_with('501')
        details = self.app._merge_api_products.call_args.args[1]
        self.assertEqual(len(details), 1)
        self.assertEqual(details[0]['_listProductIds'], ['601', '602'])
        self.assertEqual(details[0]['_listProductId'], '601')
        self.assertEqual(details[0]['_listItems'], [{'vendorItemId': '101'}])

    def test_failed_detail_uses_list_fallback_without_second_retry_loop(self):
        self.api.get_product.side_effect = ValueError('detail attempts exhausted')
        result = self.app._sync_product_data(self.api, self.db)
        self.assertFalse(result['ok'])
        self.api.get_product.assert_called_once_with('501')
        self.app._merge_api_products.assert_called_once_with(self.db, [])
        failures = self.app._merge_failed_list_products.call_args.args[1]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]['product']['sellerProductId'], '501')
        self.assertEqual(failures[0]['product']['_listProductIds'], ['601', '602'])
        self.assertEqual(failures[0]['error'], 'detail attempts exhausted')
        self.assertNotIn(2.0, [call.args[0] for call in self.api.wait.call_args_list])


if __name__ == '__main__':
    unittest.main()
