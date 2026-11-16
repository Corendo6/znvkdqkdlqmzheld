"""Run the UI synchronization worker with a real temporary ledger, no network/Tk."""
import ast
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
import queue
import threading
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import rocket_sales
from test_daily_sales import MemoryDB, VENDOR
import test_sales_shortcut as shortcut


class Cancelled(Exception):
    pass


class WorkerCollectionTests(unittest.TestCase):
    def setUp(self):
        self.db = MemoryDB()
        self.addCleanup(self.db.conn.close)
        self.db.close = Mock()
        def set_setting(key, value):
            with self.db.conn:
                self.db.conn.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, value))
        self.db.set_setting = set_setting
        self.api = SimpleNamespace(vendor_id=VENDOR, check_cancelled=lambda: None)
        self.threading = SimpleNamespace(Thread=Mock(return_value=Mock()), Event=threading.Event)
        self.namespace = dict(rocket_sales=rocket_sales, timedelta=timedelta,
            threading=self.threading, time=SimpleNamespace(monotonic=lambda: 10),
            messagebox=Mock(), APP_NAME='Test', DB_PATH='memory', Database=lambda *a, **k: self.db,
            CoupangAPI=lambda *a: self.api, CoupangAPICancelled=Cancelled, LOGGER=Mock(),
            rocket_inventory=Mock(), protect_secret=lambda s: s, unprotect_secret=lambda s: s)
        self.app = SimpleNamespace(api_busy=False, db=self.db, api_queue=queue.Queue(),
            _set_api_status=Mock(), _sync_product_data=Mock(),
            save_api_settings=lambda **kwargs: True,
            _credentials_from_db=lambda: (VENDOR, 'fake', 'fake'), _update_api_controls=Mock())
        self.app.api_sync_all = MethodType(shortcut.handlers(self.namespace)['api_sync_all'], self.app)
        self.today = date(2026, 11, 13)
        self.clock = patch.object(rocket_sales, 'today_kst', return_value=self.today)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.stamps = patch.object(rocket_sales, 'datetime', wraps=datetime)
        self.stamps.start().now.return_value = datetime(2026, 11, 13, 12, tzinfo=rocket_sales.KST)
        self.addCleanup(self.stamps.stop)
        self.calls = []

    def order_for(self, day):
        return [dict(order_id=day.replace('-', ''), paid_date=day, items={
            '101': dict(option_id='101', product_name='Test', quantity=3, revenue=Decimal(30))})]

    def run_worker(self, fetch, *, force=False):
        self.app.api_busy = False
        self.app.api_sync_all(scope='로켓그로스 일일 매출만', sales_from='2026-11-10',
                              sales_to='2026-11-12', sales_force=force)
        with patch.object(rocket_sales, 'fetch_orders', side_effect=fetch):
            self.threading.Thread.call_args.kwargs['target']()
        messages = []
        while not self.app.api_queue.empty():
            messages.append(self.app.api_queue.get_nowait())
        self.assertEqual(messages[-1][0], 'sync_complete')
        return messages

    def test_partial_failure_is_reported_and_next_run_keeps_completed_day(self):
        def first_attempt(api, start, end, *args):
            self.assertEqual(start, end)
            self.calls.append(start)
            if len(self.calls) == 2:
                raise ValueError('simulated quota limit')
            return self.order_for(start)
        messages = self.run_worker(first_attempt)
        results, cancelled = messages[-1][1:]
        self.assertFalse(results['orders']['ok'])
        self.assertFalse(cancelled)
        self.assertIn(('orders_saved',), messages)
        saved_day = self.calls[0]
        self.assertEqual(self.db.conn.execute('SELECT sale_date FROM rg_sales_coverage').fetchall()[0][0], saved_day)
        self.assertEqual(self.db.conn.execute('SELECT quantity FROM rg_order_items').fetchone()[0], 3)
        second_calls = []
        def second_attempt(api, start, end, *args):
            second_calls.append(start)
            return self.order_for(start)
        messages = self.run_worker(second_attempt)
        self.assertTrue(messages[-1][1]['orders']['ok'])
        self.assertNotIn(saved_day, second_calls)
        self.assertEqual(len(second_calls), 2)
        self.assertEqual(self.db.conn.execute('SELECT COUNT(*) FROM rg_sales_coverage').fetchone()[0], 3)
        self.assertEqual(self.db.conn.execute('SELECT SUM(quantity) FROM rg_order_items').fetchone()[0], 9)

    def test_cancellation_reports_cancelled_while_preserving_committed_dates(self):
        def fetch(api, start, end, *args):
            self.calls.append(start)
            if len(self.calls) == 2:
                raise Cancelled('cancelled by user')
            return self.order_for(start)
        messages = self.run_worker(fetch)
        self.assertTrue(messages[-1][2])
        self.assertEqual(self.db.conn.execute('SELECT COUNT(*) FROM rg_sales_coverage').fetchone()[0], 1)
        self.assertIn(('orders_saved',), messages)

    def test_force_collects_already_saved_dates_without_duplicate_totals(self):
        def fetch(api, start, end, *args):
            self.calls.append(start)
            return self.order_for(start)
        self.run_worker(fetch)
        self.calls.clear()
        self.run_worker(fetch)
        self.assertEqual(self.calls, [])
        messages = self.run_worker(fetch, force=True)
        self.assertTrue(messages[-1][1]['orders']['ok'])
        self.assertEqual(set(self.calls), {'2026-11-10', '2026-11-11', '2026-11-12'})
        self.assertEqual(self.db.conn.execute('SELECT SUM(quantity) FROM rg_order_items').fetchone()[0], 9)


class CollectionStatusLifetimeTests(unittest.TestCase):
    def test_idle_api_status_does_not_hide_collection_completeness(self):
        path = Path(__file__).resolve().parents[1] / 'coupang_manager.pyw'
        app_class = next(n for n in ast.parse(path.read_text(encoding='utf-8-sig')).body
                         if isinstance(n, ast.ClassDef) and n.name == 'CoupangManager')
        method = next(n for n in app_class.body if isinstance(n, ast.FunctionDef)
                      and n.name == '_update_api_status')
        namespace = dict(time=SimpleNamespace(monotonic=lambda: 20))
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), 'exec'), namespace)
        label = Mock()
        app = SimpleNamespace(api_last_status='done', api_busy=False, current_page='sales',
                              api_status_var=Mock(), sales_api_status=label)
        namespace['_update_api_status'](app)
        label.configure.assert_not_called()


if __name__ == '__main__':
    unittest.main()
