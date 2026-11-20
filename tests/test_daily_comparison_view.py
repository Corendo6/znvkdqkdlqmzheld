"""Window checks against an isolated database, without API calls."""
import sys
import sqlite3
import tkinter as tk
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import daily_comparison_view
import rocket_sales
from test_daily_sales import MemoryDB, VENDOR


class ComparisonViewTests(unittest.TestCase):
    def setUp(self):
        try:
            self.app = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.addCleanup(self.app.destroy)
        self.app.geometry('1600x900')
        self.app.db = MemoryDB()
        self.addCleanup(self.app.db.conn.close)
        self.app.db.setting = lambda key, default='': VENDOR if key == 'api_vendor_id' else default
        self.app.api_busy = False
        self.app.multitap_only_enabled = lambda: False
        self.app.sync_recent_daily_sales = lambda: self.fail('Rendering must not call API')
        self.errors = []
        self.app.report_callback_exception = lambda *args: self.errors.append(args)
        self.today = rocket_sales.today_kst()

    def add_orders(self, days_ago, oid, qty, amount):
        day = (self.today-timedelta(days=days_ago)).isoformat()
        self.app.db.conn.execute('INSERT OR REPLACE INTO rg_sales_coverage VALUES (?,?,?)',
            (VENDOR, day, day+'T12:00:00+09:00'))
        self.app.db.conn.execute('INSERT INTO rg_order_items VALUES (?,?,?,?,?,?,?,?)',
            (VENDOR, day+oid, oid, day, '개별 메인 멀티탭 3구 + 벨크로 타이', qty, amount, day))
        self.app.db.conn.commit()

    def open(self):
        window = daily_comparison_view.ComparisonWindow(self.app)
        self.app.update()
        return window

    def test_missing_days_are_not_zero_and_no_network_or_writes(self):
        before = self.app.db.conn.total_changes
        win = self.open()
        self.assertEqual(win.cards['revenue'][0].cget('text'), '—')
        self.assertIn('미수집', win.status.cget('text'))
        self.assertFalse(win.report['comparable'])
        self.assertEqual(self.app.db.conn.total_changes, before)
        self.assertFalse(self.errors)

    def test_multiple_windows_sort_search_reset_and_cleanup(self):
        for day, oid, qty, rev in [(2,'101',10,'100'), (1,'101',1,'10'),
                                  (2,'102',1,'10'), (1,'102',4,'40')]:
            self.add_orders(day, oid, qty, rev)
        one, two = self.open(), self.open()
        self.assertIsNone(self.app.grab_current())
        self.assertTrue(one.winfo_exists() and two.winfo_exists())
        self.assertEqual(one.cards['quantity'][0].cget('text'), '5개')
        values = [one.all_table.item(i, 'values') for i in one.all_table.get_children()]
        self.assertEqual([r[2] for r in values], ['101', '102'])
        one.query.set('102'); one.render_all()
        self.assertEqual(len(one.all_table.get_children()), 1)
        self.assertEqual(len(two.all_table.get_children()), 2)
        one.clear_search()
        self.assertEqual(len(one.all_table.get_children()), 2)
        job = one._job
        one.destroy(); self.app.update()
        self.assertNotIn(job, self.app.tk.call('after', 'info'))
        self.assertFalse(self.errors)

    def test_saved_data_refreshes_open_window_without_api(self):
        self.add_orders(2, '101', 2, '20')
        win = self.open()
        self.add_orders(1, '101', 4, '40')
        # Run one scheduled poll deterministically, without waiting two seconds.
        win.after_cancel(win._job)
        win._poll()
        self.assertTrue(win.report['comparable'])
        self.assertEqual(win.cards['revenue'][0].cget('text'), '40원')
        self.assertIn('+20원', win.cards['revenue'][1].cget('text'))
        self.app.api_busy = True
        win.after_cancel(win._job); win._poll()
        self.assertEqual(str(win.collect_button.cget('state')), 'disabled')
        self.assertFalse(self.errors)

    def test_invalid_dates_do_not_query_or_open_blocking_window_in_quiet_poll(self):
        win = self.open()
        win.base_date.set('2026-99-99')
        with patch.object(daily_comparison_view.messagebox, 'showwarning') as warning:
            self.assertFalse(win.refresh(quiet=True))
            warning.assert_not_called()
        self.assertIn('YYYY-MM-DD', win.status.cget('text'))

    def test_commit_during_refresh_is_detected_on_next_poll(self):
        self.add_orders(2, '101', 2, '20')
        self.add_orders(1, '101', 4, '40')
        win = self.open()
        original = daily_comparison_view.daily_comparison.build_report
        def report_then_commit(*args, **kwargs):
            report = original(*args, **kwargs)
            self.add_orders(1, '102', 1, '10')
            return report
        with patch.object(daily_comparison_view.daily_comparison, 'build_report', side_effect=report_then_commit):
            win.refresh()
        self.assertNotEqual(win._revision, win._current_revision())
        win.after_cancel(win._job); win._poll()
        self.assertEqual(win.cards['quantity'][0].cget('text'), '5개')

    def test_database_read_error_keeps_polling(self):
        win = self.open()
        win.after_cancel(win._job)
        with patch.object(win, '_current_revision', side_effect=sqlite3.OperationalError('busy')):
            with self.assertLogs('CoupangManager', level='ERROR'):
                win._poll()
        self.assertIn(win._job, self.app.tk.call('after', 'info'))
        self.assertIn('다시 확인', win.status.cget('text'))

    def test_multitap_scope_excludes_unrelated_option(self):
        self.add_orders(2, '101', 2, '20'); self.add_orders(1, '101', 4, '40')
        self.add_orders(1, '102', 100, '1000')
        self.app.db.conn.execute("INSERT INTO products(option_id,fulfillment_type,product_name) VALUES ('101','로켓그로스','멀티탭')")
        self.app.db.conn.commit()
        self.app.multitap_only_enabled = lambda: True
        self.app._multitap_product_clause = lambda alias: alias+".product_name='멀티탭'"
        win = self.open()
        self.assertEqual(win.cards['quantity'][0].cget('text'), '4개')
        self.assertIn('멀티탭', win.status.cget('text'))


if __name__ == '__main__':
    unittest.main()
