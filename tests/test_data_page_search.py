"""Field searches follow a sale's actual option and preserve existing visibility rules."""
import ast
from pathlib import Path
import sqlite3
import sys
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import product_search
import rocket_sales


SOURCE = Path(__file__).resolve().parents[1] / 'coupang_manager.pyw'
APP = next(n for n in ast.parse(SOURCE.read_text(encoding='utf-8-sig')).body
           if isinstance(n, ast.ClassDef) and n.name == 'CoupangManager')
NAMES = {'refresh_sales', 'refresh_model_master', 'run_search'}


class Tree:
    def __init__(self):
        self.rows = {}
    def winfo_exists(self):
        return True
    def get_children(self):
        return tuple(self.rows)
    def delete(self, *items):
        for item in items:
            self.rows.pop(item, None)
    def insert(self, parent, position, iid, values):
        self.rows[iid] = values


class DataPageSearchTests(unittest.TestCase):
    def setUp(self):
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        conn.executescript('''
            CREATE TABLE products(id INTEGER PRIMARY KEY,model TEXT,product_name TEXT,
                display_name TEXT,option_name TEXT,option_id TEXT,registered_product_id TEXT,
                exposed_product_id TEXT,seller_product_item_id TEXT,fulfillment_type TEXT,
                sale_status TEXT,status TEXT,gift TEXT,sale_price REAL,coupon_price REAL,
                map_price REAL,price_diff REAL);
            CREATE TABLE sales(id INTEGER PRIMARY KEY,sale_date TEXT,model TEXT,option_id TEXT,
                option_name TEXT,product_name TEXT,registered_product_id TEXT,method TEXT,
                revenue REAL,orders INTEGER,quantity INTEGER,source TEXT);
            CREATE TABLE model_master(model TEXT PRIMARY KEY,product_name TEXT,category TEXT,
                color TEXT,package_type TEXT,pack_qty REAL,supply_price REAL,coupang_price REAL,
                source_sheet TEXT);
            CREATE TABLE option_model_map(option_id TEXT PRIMARY KEY,model TEXT);
            CREATE TABLE unmatched_options(option_id TEXT PRIMARY KEY,option_name TEXT,
                product_name TEXT,registered_product_id TEXT,method TEXT,first_seen TEXT,last_seen TEXT);
            CREATE TABLE sales_pending(id INTEGER PRIMARY KEY);
        ''')
        self.conn = conn
        db = SimpleNamespace(conn=conn, query=lambda sql, args=(): conn.execute(sql, args).fetchall(),
            one=lambda sql, args=(): conn.execute(sql, args).fetchone(),
            setting=lambda key, default='': 'VENDOR' if key == 'api_vendor_id' else default)
        self.query = ''
        self.field = '전체'
        variable = SimpleNamespace(get=lambda: self.query)
        self.app = SimpleNamespace(db=db, sales_q=variable, model_q=variable, search_q=variable,
            _search_field=lambda page: self.field, sales_tree=Tree(), model_tree=Tree(),
            unmatched_tree=Tree(), search_tree=Tree(), sales_api_status=Mock(), model_summary=Mock(),
            search_label=Mock(), _sync_option_model_map_from_products=Mock(),
            multitap_only_enabled=Mock(return_value=False), visible_models=Mock(return_value=['MODEL-A']),
            _multitap_product_clause=lambda alias: f"{alias}.model='MODEL-A'",
            _resolved_product_link=lambda row: '')
        scope = dict(product_search=product_search, rocket_sales=rocket_sales, money=str)
        methods = [n for n in APP.body if isinstance(n, ast.FunctionDef) and n.name in NAMES]
        exec(compile(ast.Module(body=methods, type_ignores=[]), str(SOURCE), 'exec'), scope)
        for name in NAMES:
            setattr(self.app, name, MethodType(scope[name], self.app))
        self.product('101', 'MODEL-A', '등록하나', '노출하나', '2001', '3001')
        self.product('102', 'MODEL-A', '등록둘', '노출둘', '2002', '3002')
        self.product('103', 'MODEL-B', '등록셋', '노출셋', '2003', '3003')
        self.conn.executemany('INSERT INTO model_master(model,product_name,category) VALUES (?,?,?)',
            [('MODEL-A', '마스터하나', '분류하나'), ('MODEL-B', '마스터둘', '분류둘')])
        self.sale('101', 'MODEL-A')
        self.sale('102', 'MODEL-A')
        self.sale('', 'MODEL-A')
        self.sale('103', 'MODEL-B')

    def product(self, oid, model, name, display, registered, exposed):
        self.conn.execute('''INSERT INTO products(model,product_name,display_name,option_id,
            registered_product_id,exposed_product_id,option_name,fulfillment_type)
            VALUES (?,?,?,?,?,?,?,'로켓그로스')''', (model, name, display, oid, registered, exposed, '옵션'+oid))

    def sale(self, oid, model, source='manual'):
        return self.conn.execute('''INSERT INTO sales(sale_date,option_id,model,option_name,
            product_name,registered_product_id,method,revenue,orders,quantity,source)
            VALUES ('2026-11-01',?,?,'저장옵션','저장등록명','저장등록ID','로켓그로스',10,1,1,?)''',
            (oid, model, source)).lastrowid

    def sales(self, field, query):
        self.field, self.query = field, query
        self.app.refresh_sales()
        return set(self.app.sales_tree.rows)

    def master(self, field, query):
        self.field, self.query = field, query
        self.app.refresh_model_master()
        return set(self.app.model_tree.rows)

    def test_sales_searches_all_catalog_fields_and_does_not_cross_same_model_options(self):
        for field, term in [('노출상품ID', '3001'), ('옵션ID', '101'),
                            ('등록상품ID', '2001'), ('등록상품명', '등록하나'), ('노출상품명', '노출하나')]:
            with self.subTest(field=field):
                self.assertEqual(self.sales(field, term), {'1', '3'})
                self.assertEqual(self.sales('전체', term), {'1', '3'})
        self.assertEqual(self.sales('모델명', 'MODEL-A'), {'1', '2', '3'})
        self.assertEqual(self.sales('등록상품명', '노출하나'), set())
        self.assertEqual(self.sales('노출상품ID', '101'), set())

    def test_sales_keep_saved_fields_and_reset_query(self):
        self.assertEqual(self.sales('등록상품명', '저장등록명'), {'1', '2', '3', '4'})
        self.assertEqual(self.sales('등록상품ID', '저장등록ID'), {'1', '2', '3', '4'})
        self.assertEqual(self.sales('노출상품명', ''), {'1', '2', '3', '4'})

    def test_multitap_filter_keeps_allowed_models_and_current_vendor_unmapped_sales(self):
        self.app.multitap_only_enabled.return_value = True
        self.sale('404', '', rocket_sales.SOURCE_PREFIX+'VENDOR')
        self.sale('405', '', rocket_sales.SOURCE_PREFIX+'OTHER')
        self.assertEqual(self.sales('전체', ''), {'1', '2', '3', '5'})
        self.app.visible_models.return_value = []
        self.assertEqual(self.sales('옵션ID', '404'), {'5'})
        self.assertEqual(self.sales('전체', 'MODEL-A'), set())

    def test_master_matches_catalog_identity_and_saved_option_mapping(self):
        for field, term in [('노출상품ID', '3001'), ('옵션ID', '101'),
                            ('등록상품ID', '2001'), ('등록상품명', '등록하나'), ('노출상품명', '노출하나')]:
            with self.subTest(field=field):
                self.assertEqual(self.master(field, term), {'MODEL-A'})
        self.conn.execute("UPDATE products SET model='' WHERE option_id='103'")
        self.conn.execute("INSERT INTO option_model_map VALUES ('103','MODEL-B')")
        self.assertEqual(self.master('노출상품명', '노출셋'), {'MODEL-B'})
        self.conn.execute("INSERT INTO option_model_map VALUES ('999','MODEL-B')")
        self.assertEqual(self.master('옵션ID', '999'), {'MODEL-B'})
        self.assertEqual(self.master('등록상품명', '노출하나'), set())
        self.assertEqual(self.master('전체', '분류하나'), {'MODEL-A'})
        self.assertEqual(self.master('노출상품ID', ''), {'MODEL-A', 'MODEL-B'})

    def test_unmatched_master_rows_follow_query_and_catalog_names(self):
        self.conn.executemany('''INSERT INTO unmatched_options(option_id,product_name,registered_product_id)
            VALUES (?,?,?)''', [('101', '미연결등록하나', '2001'), ('103', '미연결등록셋', '2003')])
        self.master('노출상품명', '노출하나')
        self.assertEqual(set(self.app.unmatched_tree.rows), {'101'})
        self.master('등록상품ID', '2003')
        self.assertEqual(set(self.app.unmatched_tree.rows), {'103'})
        self.master('전체', '')
        self.assertEqual(set(self.app.unmatched_tree.rows), {'101', '103'})

    def test_legacy_search_honors_field_and_empty_reset(self):
        self.field, self.query = '노출상품명', '노출하나'
        self.app.run_search()
        self.assertEqual(set(self.app.search_tree.rows), {'1'})
        self.field = '등록상품명'
        self.app.run_search()
        self.assertEqual(set(self.app.search_tree.rows), set())
        self.query = ''
        self.app.run_search()
        self.assertEqual(set(self.app.search_tree.rows), {'1', '2', '3'})


if __name__ == '__main__':
    unittest.main()
