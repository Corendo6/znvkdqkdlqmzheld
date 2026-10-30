"""Check bulk sales aggregation and mapping updates without timing-sensitive assertions."""
import sys
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rocket_sales
from test_daily_sales import MemoryDB, TODAY, VENDOR


class SalesAggregationTests(unittest.TestCase):
    def setUp(self):
        self.db = MemoryDB()
        self.conn = self.db.conn
        self.addCleanup(self.conn.close)
        self.first = (TODAY - timedelta(days=30)).isoformat()
        self.last = (TODAY - timedelta(days=1)).isoformat()

    def raw(self, day, oid, order, revenue='0.1', quantity=1, vendor=VENDOR, name='Product'):
        self.conn.execute('INSERT INTO rg_order_items VALUES (?,?,?,?,?,?,?,?)',
                          (vendor, order, oid, day, name, quantity, revenue, self.last))

    def sale(self, day, oid, model='', source='manual'):
        self.conn.execute('''INSERT INTO sales(sale_date,option_id,model,method,
            revenue,orders,quantity,source) VALUES (?,?,?,'로켓그로스',0,0,0,?)''',
                          (day, oid, model, source))

    def test_decimal_totals_and_order_counts_across_options_and_vendors(self):
        for i in range(10):
            self.raw(self.last, '101', str(i))
            self.raw(self.last, '102', str(i), revenue='0.2', quantity=2)
            self.raw(self.last, '101', str(i), vendor='OTHER', revenue='9999')
        result = rocket_sales.rebuild_sales(self.conn, VENDOR)
        rows = self.conn.execute('SELECT option_id,revenue,orders,quantity FROM sales ORDER BY option_id').fetchall()
        self.assertEqual([tuple(row) for row in rows], [('101', 1.0, 10, 10), ('102', 2.0, 10, 20)])
        self.assertEqual(result['inserted'], 2)

    def test_mapping_writes_scale_with_options_and_keep_latest_unknown_name(self):
        self.conn.executemany('INSERT INTO option_model_map VALUES (?,?)', [('101', 'M1'), ('102', 'M2')])
        for days_ago in range(1, 31):
            day = (TODAY - timedelta(days=days_ago)).isoformat()
            for oid in ('101', '102', '103'):
                self.raw(day, oid, str(days_ago), name='Name ' + day)
        statements = []
        self.conn.set_trace_callback(statements.append)
        result = rocket_sales.rebuild_sales(self.conn, VENDOR, self.first, self.last)
        self.conn.set_trace_callback(None)
        writes = [sql for sql in statements if 'unmatched_options' in sql and sql.startswith(('INSERT', 'DELETE'))]
        self.assertEqual(len(writes), 3)
        self.assertEqual(result['inserted'], 90)
        self.assertEqual(result['unmapped'], 30)
        row = self.conn.execute('SELECT option_id,option_name FROM unmatched_options').fetchone()
        self.assertEqual(tuple(row), ('103', 'Name ' + self.last))

    def test_manual_option_model_and_pending_conflicts_still_take_precedence(self):
        for oid in ('101', '102', '103', '104', '105', '106'):
            self.raw(self.last, oid, '1')
        self.conn.executemany('INSERT INTO option_model_map VALUES (?,?)',
                             [('102', 'M2'), ('103', 'M3'), ('104', 'M4')])
        self.sale(self.last, '101')
        self.sale(self.last, '', 'M2')
        self.sale(self.last, 'different-option', 'M3')
        self.sale(self.last, '', '')
        self.sale(self.first, '', 'M4')
        self.conn.execute('INSERT INTO sales_pending VALUES (?, ?, ?)', (self.last, '105', '로켓그로스'))
        result = rocket_sales.rebuild_sales(self.conn, VENDOR)
        options = {row[0] for row in self.conn.execute('SELECT option_id FROM sales WHERE source=?',
                                                     (rocket_sales.SOURCE_PREFIX + VENDOR,))}
        self.assertEqual(options, {'103', '104', '106'})
        self.assertEqual(result['conflicts'], 3)

    def test_subset_keeps_other_options_and_removes_duplicate_owned_rows(self):
        source = rocket_sales.SOURCE_PREFIX + VENDOR
        self.raw(self.last, '101', '1', revenue='12.3', quantity=3)
        self.raw(self.last, '102', '1', revenue='99', quantity=9)
        self.sale(self.last, '101', source=source)
        first_id = self.conn.execute('SELECT id FROM sales').fetchone()[0]
        self.sale(self.last, '101', source=source)
        self.sale(self.last, '102', source=source)
        result = rocket_sales.rebuild_sales(self.conn, VENDOR, self.first, self.last, option_ids=['101'])
        rows = self.conn.execute('SELECT id,option_id,revenue,quantity FROM sales ORDER BY id').fetchall()
        self.assertEqual(tuple(rows[0]), (first_id, '101', 12.3, 3))
        self.assertEqual(tuple(rows[1])[1:], ('102', 0.0, 0))
        self.assertEqual(result['updated'], 1)
        self.assertEqual(len(rows), 2)

    def test_failed_batched_mapping_write_rolls_back_orders_and_daily_rows(self):
        self.conn.execute('''CREATE TRIGGER reject_mapping BEFORE INSERT ON unmatched_options
            BEGIN SELECT RAISE(ABORT, 'test mapping write failure'); END''')
        self.conn.commit()
        orders = [dict(order_id='1', paid_date=self.last, items={
            '101': dict(product_name='Product', quantity=1, revenue=Decimal('0.1'))})]
        with patch.object(rocket_sales, 'today_kst', return_value=TODAY):
            with self.assertRaisesRegex(Exception, 'test mapping write failure'):
                rocket_sales.save_orders(self.conn, VENDOR, orders, self.first, self.last)
        for table in ('rg_order_items', 'sales', 'rg_sales_coverage'):
            self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
