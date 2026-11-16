"""Daily commits survive failed requests and retain unknown versus confirmed zero."""
import sqlite3
import sys
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rocket_sales
from test_daily_sales import MemoryDB, VENDOR


TODAY = date(2026, 11, 13)


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 11, 13, 12, tzinfo=rocket_sales.KST)


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.db = MemoryDB()
        self.conn = self.db.conn
        self.addCleanup(self.conn.close)
        for name, value in [('today_kst', lambda: TODAY), ('datetime', FrozenDatetime)]:
            context = patch.object(rocket_sales, name, value)
            context.start()
            self.addCleanup(context.stop)
        self.api = SimpleNamespace(vendor_id=VENDOR, check_cancelled=lambda: None)
        self.first, self.last = self.day(3), self.day(1)

    def day(self, offset):
        return (TODAY - timedelta(days=offset)).isoformat()

    def order(self, day, quantity=3):
        return dict(order_id=day.replace('-', ''), paid_date=day, items={
            '101': dict(option_id='101', quantity=quantity, revenue=Decimal(quantity * 100), product_name='Test')})

    def cover(self, offset, stamp='2026-11-13T11:00:00+09:00', vendor=VENDOR):
        self.conn.execute('INSERT OR REPLACE INTO rg_sales_coverage VALUES (?,?,?)',
                          (vendor, self.day(offset), stamp))
        self.conn.commit()

    def status(self):
        return rocket_sales.coverage_status(self.conn, VENDOR, self.first, self.last)

    def test_failure_keeps_complete_day_and_restart_only_fetches_remaining(self):
        def fetch(api, start, end, progress=None):
            self.assertEqual(start, end)
            if start == self.day(2):
                raise RuntimeError('network failed')
            return [self.order(start)]
        saved = []
        with patch.object(rocket_sales, 'fetch_orders', side_effect=fetch):
            with self.assertRaises(rocket_sales.CollectionError) as caught:
                rocket_sales.collect_orders(self.conn, self.api, self.first, self.last, on_saved=saved.append)
        result = caught.exception.result
        self.assertEqual(result['completed_dates'], [self.last])
        self.assertEqual(result['remaining_dates'], [self.day(2), self.first])
        self.assertEqual(saved, [self.last])
        self.assertEqual(self.status()['completed_dates'], [self.last])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM rg_order_items').fetchone()[0], 1)
        self.assertIn(self.day(2), self.db.setting('rg_orders_last_error:' + VENDOR))
        with patch.object(rocket_sales, 'fetch_orders', return_value=[]) as fetch:
            resumed = rocket_sales.collect_orders(self.conn, self.api, self.first, self.last)
        self.assertEqual([call.args[1] for call in fetch.call_args_list], [self.day(2), self.first])
        self.assertTrue(resumed['coverage']['complete'])
        self.assertEqual(self.db.setting('rg_orders_last_error:' + VENDOR), '')
        self.assertEqual(self.db.setting('rg_orders_failed_date:' + VENDOR), '')

    def test_zero_days_are_complete_unknown_days_are_not_zero(self):
        before = rocket_sales.daily_quantities(self.conn, VENDOR, TODAY)
        self.assertIsNone(before['empty']['yesterday'])
        with patch.object(rocket_sales, 'fetch_orders', return_value=[]):
            rocket_sales.collect_orders(self.conn, self.api, self.last, self.last)
        after = rocket_sales.daily_quantities(self.conn, VENDOR, TODAY)
        self.assertEqual(after['empty']['yesterday'], 0)
        self.assertIsNone(after['empty']['last7'])
        self.assertTrue(after['status'][1]['complete'])

    def test_missing_before_stale_recent_and_old_covered_is_reused(self):
        self.cover(1, '2026-11-12T12:00:00+09:00')
        self.cover(2)
        self.cover(7, '2026-11-12T12:00:00+09:00')
        self.cover(8, '2026-11-12T12:00:00+09:00')
        with patch.object(rocket_sales, 'fetch_orders', return_value=[]) as fetch:
            result = rocket_sales.collect_orders(self.conn, self.api, self.day(8), self.last)
        self.assertEqual([call.args[1] for call in fetch.call_args_list],
                         [self.day(i) for i in (3, 4, 5, 6, 1, 7)])
        self.assertEqual(result['skipped_dates'], [self.day(8), self.day(2)])
        with patch.object(rocket_sales, 'fetch_orders') as fetch:
            again = rocket_sales.collect_orders(self.conn, self.api, self.day(8), self.last)
        fetch.assert_not_called()
        self.assertEqual(len(again['skipped_dates']), 8)

    def test_force_refresh_and_vendor_isolation(self):
        self.cover(1)
        self.cover(2, vendor='OTHER')
        self.cover(3)
        with patch.object(rocket_sales, 'fetch_orders', return_value=[]) as fetch:
            rocket_sales.collect_orders(self.conn, self.api, self.first, self.last, force=True)
        self.assertEqual([call.args[1] for call in fetch.call_args_list], [self.day(2), self.last, self.first])
        self.assertEqual(rocket_sales.coverage_status(self.conn, 'OTHER', self.first, self.last)['count'], 1)

    def test_cancel_after_first_commit_preserves_progress_and_original_error(self):
        class Cancelled(RuntimeError):
            pass
        cancelled = False
        def check():
            if cancelled:
                raise Cancelled('사용자가 취소했습니다')
        def on_saved(day):
            nonlocal cancelled
            self.assertFalse(self.conn.in_transaction)
            self.assertEqual(self.status()['count'], 1)
            cancelled = True
        self.api.check_cancelled = check
        with patch.object(rocket_sales, 'fetch_orders', return_value=[]):
            with self.assertRaises(rocket_sales.CollectionError) as caught:
                rocket_sales.collect_orders(self.conn, self.api, self.first, self.last, on_saved=on_saved)
        self.assertIsInstance(caught.exception.cause, Cancelled)
        self.assertEqual(self.status()['count'], 1)
        self.assertEqual(caught.exception.result['remaining_dates'], [self.day(2), self.first])

    def test_failed_save_rolls_back_raw_sales_and_coverage_only_for_that_day(self):
        self.conn.execute('''CREATE TRIGGER reject_second_day BEFORE INSERT ON rg_sales_coverage
            WHEN NEW.sale_date='2026-11-11'
            BEGIN SELECT RAISE(ABORT, 'test write failure'); END''')
        with patch.object(rocket_sales, 'fetch_orders', side_effect=lambda api, start, end, progress: [self.order(start)]):
            with self.assertRaises(rocket_sales.CollectionError):
                rocket_sales.collect_orders(self.conn, self.api, self.first, self.last)
        self.assertEqual(self.status()['completed_dates'], [self.last])
        self.assertEqual([row[0] for row in self.conn.execute('SELECT paid_date FROM rg_order_items')], [self.last])
        self.assertEqual([row[0] for row in self.conn.execute('SELECT sale_date FROM sales')], [self.last])

    def test_vendor_change_during_fetch_cannot_save_under_old_vendor(self):
        def fetch(*args):
            self.api.vendor_id = 'OTHER'
            return [self.order(self.last)]
        with patch.object(rocket_sales, 'fetch_orders', side_effect=fetch):
            with self.assertRaises(rocket_sales.CollectionError):
                rocket_sales.collect_orders(self.conn, self.api, self.last, self.last)
        self.assertEqual(self.status()['count'], 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM rg_order_items').fetchone()[0], 0)

    def test_other_pending_transaction_is_not_committed(self):
        self.conn.execute("INSERT INTO settings VALUES ('pending', 'x')")
        with patch.object(rocket_sales, 'fetch_orders') as fetch:
            with self.assertRaises(ValueError):
                rocket_sales.collect_orders(self.conn, self.api, self.first, self.last)
        fetch.assert_not_called()
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertEqual(self.db.setting('pending'), '')

    def test_legacy_checkpoint_revalidated_once_without_deleting_orders(self):
        rocket_sales.save_orders(self.conn, VENDOR, [self.order(self.last)], self.last, self.last)
        self.conn.execute("DELETE FROM settings WHERE key='rg_orders_coverage_version'")
        rocket_sales.ensure_schema(self.conn)
        self.conn.commit()
        status = self.status()
        self.assertEqual(status['unverified_dates'], [self.last])
        self.assertEqual(status['never_collected_dates'], [self.first, self.day(2)])
        self.assertEqual(status['count'], 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM rg_order_items').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM sales').fetchone()[0], 1)
        with patch.object(rocket_sales, 'fetch_orders', return_value=[self.order(self.last)]):
            rocket_sales.collect_orders(self.conn, self.api, self.last, self.last)
        rocket_sales.ensure_schema(self.conn)
        self.conn.commit()
        self.assertEqual(self.status()['unverified_dates'], [])
        self.assertEqual(self.status()['completed_dates'], [self.last])

    def test_stale_detection_uses_korean_date_and_invalid_stamp_refreshes(self):
        self.cover(1, '2026-11-12T15:00:00+00:00')  # Today midnight KST.
        self.cover(2, 'not-a-date')
        with patch.object(rocket_sales, 'fetch_orders', return_value=[]) as fetch:
            rocket_sales.collect_orders(self.conn, self.api, self.day(2), self.last)
        self.assertEqual([call.args[1] for call in fetch.call_args_list], [self.day(2)])

    def test_interrupted_force_refresh_resumes_old_days_after_restart(self):
        for offset in (15, 16, 17):
            self.cover(offset)
        class ProcessStopped(BaseException):
            pass
        def stop(day):
            raise ProcessStopped()
        with patch.object(rocket_sales, 'fetch_orders', return_value=[]):
            with self.assertRaises(ProcessStopped):
                rocket_sales.collect_orders(self.conn, self.api, self.day(17), self.day(15),
                    force=True, on_saved=stop)
        self.assertEqual(self.db.setting('rg_orders_remaining_dates:' + VENDOR),
                         ','.join([self.day(16), self.day(17)]))
        with patch.object(rocket_sales, 'fetch_orders', return_value=[]) as fetch:
            rocket_sales.collect_orders(self.conn, self.api, self.day(17), self.day(15))
        self.assertEqual([call.args[1] for call in fetch.call_args_list], [self.day(16), self.day(17)])

    def test_querying_another_period_preserves_outside_pending_dates(self):
        pending = self.day(20)
        self.conn.execute('INSERT INTO settings VALUES (?,?)',
                          ('rg_orders_remaining_dates:' + VENDOR, pending))
        self.conn.commit()
        with patch.object(rocket_sales, 'fetch_orders', return_value=[]):
            rocket_sales.collect_orders(self.conn, self.api, self.last, self.last)
        self.assertEqual(self.db.setting('rg_orders_remaining_dates:' + VENDOR), pending)
        self.assertIn('미완료', self.db.setting('rg_orders_last_error:' + VENDOR))

    def test_old_settings_only_upgrade_requires_revalidation(self):
        self.conn.execute('DROP TABLE rg_sales_coverage')
        self.conn.execute("DELETE FROM settings WHERE key='rg_orders_coverage_version'")
        self.conn.executemany('INSERT OR REPLACE INTO settings VALUES (?,?)', [
            ('rg_orders_last_sync:' + VENDOR, '2026-11-01T12:00:00+09:00'),
            ('rg_orders_last_from:' + VENDOR, self.first),
            ('rg_orders_last_to:' + VENDOR, self.last)])
        rocket_sales.ensure_schema(self.conn)
        self.conn.commit()
        self.assertEqual(self.status()['count'], 0)
        self.assertEqual(self.status()['unverified_dates'], [self.first, self.day(2), self.last])


if __name__ == '__main__':
    unittest.main()
