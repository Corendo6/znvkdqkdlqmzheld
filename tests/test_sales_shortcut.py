"""Exercise real UI handler methods without importing or opening the Tk app."""
import ast
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rocket_sales


SOURCE = Path(__file__).resolve().parents[1] / 'coupang_manager.pyw'
TREE = ast.parse(SOURCE.read_text(encoding='utf-8-sig'))
APP_CLASS = next(node for node in TREE.body if isinstance(node, ast.ClassDef) and node.name == 'CoupangManager')
HANDLERS = {'sync_recent_daily_sales', 'api_sync_all', '_credentials_from_db',
            '_credentials_from_form', 'save_api_settings', '_update_api_controls'}


def handlers(globals_dict):
    selected = [node for node in APP_CLASS.body if isinstance(node, ast.FunctionDef) and node.name in HANDLERS]
    module = ast.Module(body=selected, type_ignores=[])
    exec(compile(module, str(SOURCE), 'exec'), globals_dict)
    return {name: globals_dict[name] for name in HANDLERS}


class DailySalesShortcutTests(unittest.TestCase):
    def setUp(self):
        self.thread = Mock()
        self.threading = SimpleNamespace(Thread=Mock(return_value=self.thread),
            Event=Mock(return_value=SimpleNamespace(is_set=lambda: False)))
        self.dialogs = Mock()
        self.worker_db = Mock()
        self.api = Mock()
        self.globals = dict(rocket_sales=rocket_sales, timedelta=timedelta,
            threading=self.threading, time=SimpleNamespace(monotonic=lambda: 10),
            messagebox=self.dialogs, APP_NAME='Test', DB_PATH='unused-mock-path',
            Database=Mock(return_value=self.worker_db), CoupangAPI=Mock(return_value=self.api),
            CoupangAPICancelled=type('TestCancellation', (Exception,), {}), LOGGER=Mock(),
            rocket_inventory=Mock(), protect_secret=lambda text: text,
            unprotect_secret=lambda text: text)
        self.methods = handlers(self.globals)
        self.db = Mock()
        self.db.setting.side_effect = lambda key, default='': {
            'api_vendor_id': 'TEST-VENDOR', 'api_access_key_enc': 'test-access',
            'api_secret_key_enc': 'test-secret'}.get(key, default)
        self.app = SimpleNamespace(api_busy=False, db=self.db, api_queue=Mock(),
                                   _set_api_status=Mock(), _sync_product_data=Mock())
        for name, method in self.methods.items():
            setattr(self.app, name, MethodType(method, self.app))

    def test_shortcut_uses_kst_clock_and_exact_inclusive_30_day_period(self):
        self.app.api_sync_all = Mock()
        with patch.object(rocket_sales, 'today_kst', return_value=date(2026, 10, 29)) as clock:
            self.app.sync_recent_daily_sales()
        clock.assert_called_once_with()
        self.app.api_sync_all.assert_called_once_with(scope='로켓그로스 일일 매출만',
            sales_from='2026-09-29', sales_to='2026-10-28')

    def test_shortcut_recomputes_dates_on_each_click_across_midnight(self):
        self.app.api_sync_all = Mock()
        with patch.object(rocket_sales, 'today_kst', side_effect=[date(2026, 10, 28), date(2026, 10, 29)]):
            self.app.sync_recent_daily_sales()
            self.app.sync_recent_daily_sales()
        calls = self.app.api_sync_all.call_args_list
        self.assertEqual(calls[0].kwargs['sales_to'], '2026-10-27')
        self.assertEqual(calls[1].kwargs['sales_to'], '2026-10-28')

    def test_shortcut_starts_without_settings_page_and_runs_only_sales_worker(self):
        self.assertFalse(hasattr(self.app, 'api_scope'))
        self.assertFalse(hasattr(self.app, 'api_sales_from'))
        self.assertFalse(hasattr(self.app, 'api_vendor_id'))
        with patch.object(rocket_sales, 'today_kst', return_value=date(2026, 10, 29)):
            self.app.sync_recent_daily_sales()
        self.thread.start.assert_called_once_with()
        self.db.conn.commit.assert_called_once_with()
        self.assertTrue(self.app.api_busy)
        target = self.threading.Thread.call_args.kwargs['target']
        orders = [object()]
        saved = {'inserted': 1, 'updated': 0, 'unchanged': 0, 'conflicts': 0, 'unmapped': 0, 'orders': 1}
        with patch.object(rocket_sales, 'fetch_orders', return_value=orders) as fetch, \
             patch.object(rocket_sales, 'save_orders', return_value=saved) as save:
            target()
        fetch.assert_called_once_with(self.api, '2026-09-29', '2026-10-28', self.api.progress)
        save.assert_called_once_with(self.worker_db.conn, 'TEST-VENDOR', orders, '2026-09-29', '2026-10-28')
        self.globals['rocket_inventory'].fetch_inventory.assert_not_called()
        self.app._sync_product_data.assert_not_called()
        self.worker_db.close.assert_called_once_with()
        self.app.api_queue.put.assert_any_call(('orders_saved',))
        completed = self.app.api_queue.put.call_args.args[0]
        self.assertEqual(completed[0], 'sync_complete')
        self.assertTrue(completed[1]['orders']['ok'])
        self.assertFalse(completed[2])

    def test_busy_shortcut_does_not_launch_second_worker(self):
        self.app.api_busy = True
        with patch.object(rocket_sales, 'today_kst', return_value=date(2026, 10, 29)):
            self.app.sync_recent_daily_sales()
        self.threading.Thread.assert_not_called()
        self.db.set_setting.assert_not_called()

    def test_missing_saved_credentials_show_actionable_warning(self):
        self.db.setting.side_effect = lambda key, default='': default
        with patch.object(rocket_sales, 'today_kst', return_value=date(2026, 10, 29)):
            self.app.sync_recent_daily_sales()
        self.threading.Thread.assert_not_called()
        self.dialogs.showwarning.assert_called_once()
        self.assertIn('API 키', self.dialogs.showwarning.call_args.args[1])
        self.assertFalse(self.app.api_busy)

    def test_sales_buttons_follow_busy_state_without_settings_widgets(self):
        self.app.sales_api_sync_button = Mock()
        self.app.sales_api_cancel_button = Mock()
        for widget in (self.app.sales_api_sync_button, self.app.sales_api_cancel_button):
            widget.winfo_exists.return_value = True
        self.app._update_api_controls()
        self.app.sales_api_sync_button.configure.assert_called_with(state='normal')
        self.app.sales_api_cancel_button.configure.assert_called_with(state='disabled')
        self.app.api_busy = True
        self.app._update_api_controls()
        self.app.sales_api_sync_button.configure.assert_called_with(state='disabled')
        self.app.sales_api_cancel_button.configure.assert_called_with(state='normal')


if __name__ == '__main__':
    unittest.main()
