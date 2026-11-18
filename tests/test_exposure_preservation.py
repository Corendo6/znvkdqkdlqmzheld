"""Exposure repair uses exact options and preserves existing ledgers and links."""
import ast
from datetime import datetime
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import exposure_lookup
import rocket_inventory

SOURCE = Path(__file__).resolve().parents[1] / 'coupang_manager.pyw'
tree = ast.parse(SOURCE.read_text(encoding='utf-8-sig'))
original = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'CoupangManager')
names = {'_merge_api_products', '_apply_revenue_exposure_map', '_resolved_product_link',
         '_merge_failed_list_products',
         '_coupang_product_link', '_make_exposure_key', '_record_exposure',
         '_best_trusted_exposure', '_record_product_event', '_sibling_business_row',
         '_cleanup_legacy_api_duplicates', '_usable_internal_category', '_exposure_option_key'}
test_class = ast.ClassDef(name='App', bases=[], keywords=[], decorator_list=[],
                         body=[n for n in original.body if isinstance(n, ast.FunctionDef) and n.name in names])
namespace = dict(urllib=urllib, LOGGER=Mock(), rocket_inventory=rocket_inventory,
                 exposure_lookup=exposure_lookup, datetime=datetime)
num = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'num')
exec(compile(ast.fix_missing_locations(ast.Module(body=[num, test_class], type_ignores=[])),
             str(SOURCE), 'exec'), namespace)


class ExposurePreservationTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript('''
            CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);
            INSERT INTO settings VALUES ('api_vendor_id','V1');
            CREATE TABLE products(id INTEGER PRIMARY KEY,category TEXT DEFAULT '',
                fulfillment_type TEXT DEFAULT '로켓그로스',status TEXT DEFAULT '',
                model TEXT DEFAULT 'MODEL',product_name TEXT DEFAULT '',display_name TEXT DEFAULT '',
                registered_product_id TEXT DEFAULT '100',exposed_product_id TEXT DEFAULT '',
                option_name TEXT DEFAULT 'same option',option_id TEXT DEFAULT '',
                seller_product_item_id TEXT DEFAULT '',item_id TEXT DEFAULT '',gift TEXT DEFAULT '',
                sale_price REAL DEFAULT 100,coupon_price REAL,map_price REAL,price_diff REAL,
                link TEXT DEFAULT '',note TEXT DEFAULT '',updated_at TEXT,image_url TEXT);
            CREATE TABLE product_exposure_history(id INTEGER PRIMARY KEY,option_key TEXT,
                option_id TEXT,seller_product_item_id TEXT,registered_product_id TEXT,
                exposed_product_id TEXT,model TEXT,option_name TEXT,source TEXT,
                first_seen TEXT DEFAULT CURRENT_TIMESTAMP,last_seen TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(option_key,exposed_product_id));
            CREATE TABLE product_events(id INTEGER PRIMARY KEY,event_type TEXT,product_row_id INTEGER,
                option_key TEXT,option_id TEXT,registered_product_id TEXT,model TEXT,option_name TEXT,
                old_value TEXT,new_value TEXT,source TEXT);
            CREATE TABLE sales(id INTEGER PRIMARY KEY,quantity INTEGER,revenue TEXT);
            CREATE TABLE rg_order_items(order_id TEXT,option_id TEXT,quantity INTEGER);
            CREATE TABLE rg_inventory_snapshots(option_id TEXT,orderable_quantity INTEGER);
            INSERT INTO sales VALUES (1,17,'23456.78');
            INSERT INTO rg_order_items VALUES ('9000','101',17);
            INSERT INTO rg_inventory_snapshots VALUES ('101',23);
        ''')
        self.db = SimpleNamespace(conn=self.conn,
            query=lambda sql, args=(): self.conn.execute(sql, args).fetchall(),
            one=lambda sql, args=(): self.conn.execute(sql, args).fetchone(),
            setting=lambda key, default='': (self.conn.execute('SELECT value FROM settings WHERE key=?',
                (key,)).fetchone() or [default])[0])
        self.app = namespace['App']()
        self.app.db = self.db

    def row(self, oid='101', **values):
        data = dict(option_id=oid, seller_product_item_id='50'+oid)
        data.update(values)
        keys = list(data)
        self.conn.execute('INSERT INTO products('+','.join(keys)+') VALUES ('+','.join('?' for _ in keys)+')',
                          [data[k] for k in keys])
        self.conn.commit()
        return self.get(oid)

    def get(self, oid='101'):
        return self.conn.execute('SELECT * FROM products WHERE option_id=?', (oid,)).fetchone()

    def detail(self, oid='101', pid=None, **updates):
        channel = dict(vendorItemId=int(oid), sellerProductItemId=int('50'+oid), salePrice=100)
        if pid is not None:
            channel['productId'] = int(pid)
        result = dict(vendorId='V1', sellerProductId=100, sellerProductName='Product',
                      items=[dict(itemName='same option', rocketGrowthItemData=channel)])
        result.update(updates)
        return result

    def observed(self, oid='101', pid='800'):
        return dict(latest={oid: dict(productId=pid, seenDate='2026-11-16')},
                    history={oid: {pid: '2026-11-16'}})

    def ledger(self):
        return {name: [tuple(row) for row in self.conn.execute('SELECT * FROM '+name)]
                for name in ('sales', 'rg_order_items', 'rg_inventory_snapshots')}

    def test_revenue_fills_only_exact_option_and_keeps_other_data_unchanged(self):
        self.row('101')
        sibling = dict(self.row('102', exposed_product_id='900', link='https://example.test/manual'))
        before = self.ledger()
        self.app._apply_revenue_exposure_map(self.db, self.observed())
        row = self.get()
        self.assertEqual(row['exposed_product_id'], '800')
        self.assertEqual(row['link'], 'https://www.coupang.com/vp/products/800?vendorItemId=101')
        self.assertEqual(dict(self.get('102')), sibling)
        self.assertEqual(self.ledger(), before)

    def test_revenue_preserves_existing_id_and_canonical_manual_link(self):
        link = 'https://www.coupang.com/vp/products/700?vendorItemId=101&manual=1'
        row = self.row(exposed_product_id='700', link=link)
        self.app._record_exposure(self.db, '101', row['seller_product_item_id'], '100',
                                  '700', source='manual', row_id=row['id'])
        self.conn.commit()
        before = dict(self.get())
        self.app._apply_revenue_exposure_map(self.db, self.observed())
        self.assertEqual(dict(self.get()), before)
        self.assertEqual(self.app._resolved_product_link(self.get()), link)

    def test_revenue_does_not_replace_existing_id_without_manual_history(self):
        link = 'https://www.coupang.com/vp/products/700?vendorItemId=101'
        self.row(exposed_product_id='700', link=link)
        self.app._apply_revenue_exposure_map(self.db, self.observed())
        self.assertEqual(self.get()['exposed_product_id'], '700')
        self.assertEqual(self.get()['link'], link)

    def test_unproven_latest_mapping_is_not_persisted(self):
        self.row()
        observed = self.observed()
        observed['latest']['101']['productId'] = '999'
        self.app._apply_revenue_exposure_map(self.db, observed)
        self.assertEqual(self.get()['exposed_product_id'], '')
        self.assertEqual(self.get()['link'], '')

    def test_detail_preserves_saved_id_and_link_when_api_id_missing_or_different(self):
        link = 'https://www.coupang.com/vp/products/700?vendorItemId=101&manual=1'
        self.row(exposed_product_id='700', link=link)
        for pid in (None, '800'):
            with self.subTest(pid=pid):
                self.app._merge_api_products(self.db, [self.detail(pid=pid)])
                self.assertEqual(self.get()['exposed_product_id'], '700')
                self.assertEqual(self.get()['link'], link)

    def test_same_name_new_option_does_not_replace_existing_option_or_borrow_id(self):
        self.row('102', exposed_product_id='900', link='https://example.test/old')
        before = dict(self.get('102'))
        self.app._merge_api_products(self.db, [self.detail()])
        self.assertIsNotNone(self.get('101'))
        self.assertEqual(self.get('101')['exposed_product_id'], '')
        self.assertEqual(dict(self.get('102')), before)

    def test_marketplace_sibling_and_single_top_level_id_are_not_evidence(self):
        self.row('102', fulfillment_type='판매자배송', exposed_product_id='900')
        self.app._record_exposure(self.db, '102', '50102', '100', '900', source='manual')
        self.conn.commit()
        self.app._merge_api_products(self.db, [self.detail(productId=999, _listProductId='999')])
        self.assertEqual(self.get()['exposed_product_id'], '')
        self.assertEqual(self.get()['link'], '')

    def test_detail_exact_pair_resolves_without_touching_ledgers(self):
        before = self.ledger()
        self.app._merge_api_products(self.db, [self.detail(pid='800')])
        self.assertEqual(self.get()['exposed_product_id'], '800')
        self.assertIn('vendorItemId=101', self.get()['link'])
        self.assertEqual(self.ledger(), before)

    def test_cross_vendor_response_cannot_change_product(self):
        self.row()
        before = dict(self.get())
        with self.assertRaises(ValueError):
            self.app._merge_api_products(self.db, [self.detail(pid='800', vendorId='OTHER')])
        self.assertEqual(dict(self.get()), before)

    def test_explicit_list_pair_survives_detail_with_missing_product_id(self):
        listed=self.detail(pid='800')
        detail=self.detail(_listItems=listed['items'], _exposureListRows=[listed])
        self.app._merge_api_products(self.db, [detail])
        self.assertEqual(self.get()['exposed_product_id'], '800')

    def test_detail_and_list_conflict_does_not_pick_either_product(self):
        detail=self.detail(pid='800', _exposureListRows=[self.detail(pid='900')])
        self.app._merge_api_products(self.db, [detail])
        self.assertEqual(self.get()['exposed_product_id'], '')
        self.assertEqual(self.get()['link'], '')

    def test_ambiguous_revenue_does_not_create_trusted_history(self):
        self.row()
        result=dict(history={'101':{'800':'2026-11-16','900':'2026-11-16'}}, latest={}, ambiguous=['101'])
        self.app._apply_revenue_exposure_map(self.db,result)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM product_exposure_history').fetchone()[0],0)
        self.app._merge_api_products(self.db,[self.detail()])
        self.assertEqual(self.get()['exposed_product_id'],'')

    def test_reused_seller_item_keeps_both_distinct_option_ids(self):
        old=dict(self.row('102',seller_product_item_id='50101',exposed_product_id='900',note='쿠팡 API 자동등록'))
        self.app._merge_api_products(self.db,[self.detail()])
        self.assertEqual(dict(self.get('102')),old)
        self.assertEqual(self.get('101')['exposed_product_id'],'')

    def test_failed_detail_placeholder_cannot_generate_top_level_link(self):
        self.app._merge_failed_list_products(self.db,[dict(product=dict(sellerProductId='100',productId='999',
            sellerProductName='Unverified',_sources=['rocketGrowth']),error='failed')])
        row=self.conn.execute('SELECT * FROM products').fetchone()
        self.assertEqual(row['exposed_product_id'],'')
        self.assertEqual(self.app._resolved_product_link(row),'')


if __name__ == '__main__':
    unittest.main()
