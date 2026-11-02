"""Startup restores exact saved links without running slow name inference."""
import ast
from pathlib import Path
import sqlite3
import sys
import time
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rocket_sales
from test_daily_sales import MemoryDB


SOURCE = Path(__file__).resolve().parents[1] / 'coupang_manager.pyw'
APP = next(node for node in ast.parse(SOURCE.read_text(encoding='utf-8-sig')).body
           if isinstance(node, ast.ClassDef) and node.name == 'CoupangManager')
NAMES = {'_startup_model_repair', '_auto_repair_model_links', '_auto_link_missing_models',
         '_auto_link_pending_models', '_resolve_model_for_option', '_store_auto_model_mapping',
         '_materialize_pending_sales', '_upsert_sale_from_mapping'}


class StartupRepairTests(unittest.TestCase):
    def setUp(self):
        self.db = MemoryDB()
        self.addCleanup(self.db.conn.close)
        self.db.one = lambda sql, args=(): self.db.conn.execute(sql, args).fetchone()
        self.db.conn.executescript('''
            ALTER TABLE products ADD COLUMN display_name TEXT;
            ALTER TABLE option_model_map ADD COLUMN source TEXT;
            ALTER TABLE option_model_map ADD COLUMN updated_at TEXT;
            DROP TABLE sales_pending;
            CREATE TABLE sales_pending(id INTEGER PRIMARY KEY, sale_date TEXT, option_id TEXT,
                method TEXT, option_name TEXT, product_name TEXT, registered_product_id TEXT,
                revenue REAL, orders REAL, quantity REAL);
        ''')
        self.logger = Mock()
        scope = dict(sqlite3=sqlite3, time=time, LOGGER=self.logger, rocket_sales=rocket_sales)
        methods = [node for node in APP.body if isinstance(node, ast.FunctionDef) and node.name in NAMES]
        exec(compile(ast.Module(body=methods, type_ignores=[]), str(SOURCE), 'exec'), scope)
        self.app = SimpleNamespace(db=self.db, api_busy=False, current_page='product_dashboard',
            _set_api_status=Mock(), refresh_model_master=Mock(),
            _build_model_match_examples=Mock(side_effect=AssertionError('startup built fuzzy examples')),
            _infer_model_from_text=Mock(side_effect=AssertionError('startup inferred a name')))
        for name in NAMES:
            setattr(self.app, name, MethodType(scope[name], self.app))

    def product(self, oid, model=''):
        cursor = self.db.conn.execute('''INSERT INTO products(option_id,model,fulfillment_type,
            option_name,product_name,display_name) VALUES (?,?,'로켓그로스','White 3m','Product','Product')''',
            (oid, model))
        return cursor.lastrowid

    def mapping(self, oid, model, source='manual'):
        self.db.conn.execute('INSERT INTO option_model_map(option_id,model,source) VALUES (?,?,?)',
                             (oid, model, source))

    def pending(self, oid):
        self.db.conn.execute('''INSERT INTO sales_pending(sale_date,option_id,method,option_name,
            product_name,registered_product_id,revenue,orders,quantity)
            VALUES ('2026-10-01',?,'로켓그로스','White 3m','Product','123',12000,2,3)''', (oid,))

    def assert_no_inference(self):
        self.app._build_model_match_examples.assert_not_called()
        self.app._infer_model_from_text.assert_not_called()
        self.logger.exception.assert_not_called()

    def test_startup_restores_manual_mapping_and_materializes_its_pending_sales(self):
        product = self.product('101')
        self.mapping('101', 'MANUAL-MODEL')
        self.pending('101')
        self.app._startup_model_repair()
        self.assertEqual(self.db.one('SELECT model FROM products WHERE id=?', (product,))[0], 'MANUAL-MODEL')
        self.assertEqual(tuple(self.db.one('SELECT model,source FROM option_model_map WHERE option_id=?', ('101',))),
                         ('MANUAL-MODEL', 'manual'))
        self.assertEqual(tuple(self.db.one('SELECT model,revenue,orders,quantity FROM sales')),
                         ('MANUAL-MODEL', 12000.0, 2, 3))
        self.assertEqual(self.db.one('SELECT COUNT(*) FROM sales_pending')[0], 0)
        self.assert_no_inference()

    def test_startup_uses_exact_sibling_option_without_overwriting_existing_models(self):
        sibling = self.product('102', 'SIBLING-MODEL')
        missing = self.product('102')
        existing = self.product('103', 'KEEP-ME')
        self.mapping('103', 'OTHER-MODEL')
        self.app._startup_model_repair()
        self.assertEqual([row[0] for row in self.db.query('SELECT model FROM products ORDER BY id')],
                         ['SIBLING-MODEL', 'SIBLING-MODEL', 'KEEP-ME'])
        self.assertEqual(self.db.one('SELECT model FROM option_model_map WHERE option_id=?', ('102',))[0],
                         'SIBLING-MODEL')
        self.assert_no_inference()

    def test_startup_leaves_unknown_product_and_pending_sales_unassigned(self):
        self.product('104')
        self.pending('104')
        self.app._startup_model_repair()
        self.assertEqual(self.db.one('SELECT model FROM products')[0], '')
        self.assertEqual(self.db.one('SELECT COUNT(*) FROM option_model_map')[0], 0)
        self.assertEqual(self.db.one('SELECT COUNT(*) FROM sales_pending')[0], 1)
        self.assertEqual(self.db.one('SELECT COUNT(*) FROM sales')[0], 0)
        self.assert_no_inference()

    def test_explicit_repair_still_infers_product_and_pending_only_options(self):
        self.product('105')
        self.pending('106')
        self.app._build_model_match_examples.side_effect = None
        self.app._build_model_match_examples.return_value = [object()]
        self.app._infer_model_from_text.side_effect = [('INFERRED-PRODUCT', 1.2, 0.1),
                                                       ('INFERRED-PENDING', 1.2, 0.1)]
        result = self.app._auto_repair_model_links()
        self.assertEqual(result, dict(linked=1, ambiguous=0, pendingLinked=1, salesMoved=1))
        self.assertEqual(self.app._infer_model_from_text.call_count, 2)
        self.assertEqual(self.db.one('SELECT model FROM products')[0], 'INFERRED-PRODUCT')
        self.assertEqual(self.db.one('SELECT model FROM sales')[0], 'INFERRED-PENDING')

    def test_startup_does_not_contend_with_running_api_writer(self):
        self.product('107')
        self.mapping('107', 'MAPPED')
        self.app.api_busy = True
        self.app._auto_repair_model_links = Mock()
        self.app._startup_model_repair()
        self.app._auto_repair_model_links.assert_not_called()
        self.assertEqual(self.db.one('SELECT model FROM products')[0], '')
        self.assert_no_inference()

    def test_failed_startup_repair_is_nonfatal_and_lock_is_reported(self):
        for error in (sqlite3.OperationalError('database is locked'), ValueError('bad data')):
            with self.subTest(error=type(error).__name__):
                self.app._auto_repair_model_links = Mock(side_effect=error)
                self.app._startup_model_repair()
        self.app._set_api_status.assert_called_once()
        self.logger.warning.assert_called_once()
        self.logger.exception.assert_called_once()


if __name__ == '__main__':
    unittest.main()
