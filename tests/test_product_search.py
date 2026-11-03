"""Keep all F5 product fields consistent between database and in-memory views."""
import sqlite3
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import product_search


class ProductSearchTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.addCleanup(self.conn.close)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('CREATE TABLE products(id INTEGER PRIMARY KEY,' +
                          ','.join(column + ' TEXT' for column in product_search.ALL_COLUMNS) + ')')
        self.rows = [
            dict(id=1, exposed_product_id='8111222333', option_id='9111222333',
                 registered_product_id='7111222333', model='HMP35L',
                 product_name='현대 등록 멀티탭', display_name='현대 노출 절전상품',
                 option_name='화이트 3m', seller_product_item_id='6111222333'),
            dict(id=2, exposed_product_id='8111000222', option_id='9555666777',
                 registered_product_id='7555666777', model='HMP123456',
                 product_name=r'할인 50%_행사\상품', display_name=None,
                 option_name='블랙', seller_product_item_id='6555666777'),
            dict(id=3, model='앞', product_name='뒤'),
            dict(id=4),
        ]
        for row in self.rows:
            columns = tuple(row)
            self.conn.execute('INSERT INTO products(' + ','.join(columns) + ') VALUES (' +
                              ','.join('?' for _ in columns) + ')', tuple(row.values()))

    def assert_results(self, query, expected, field='전체'):
        clause, params = product_search.sql_clause(query, field)
        actual = [row['id'] for row in self.conn.execute(
            'SELECT p.* FROM products p WHERE ' + clause + ' ORDER BY id', params)]
        self.assertEqual(actual, expected)
        self.assertEqual([row['id'] for row in self.rows if product_search.matches(row, query, field)], expected)
        sqlite_rows = self.conn.execute('SELECT * FROM products ORDER BY id').fetchall()
        self.assertEqual([row['id'] for row in sqlite_rows if product_search.matches(row, query, field)], expected)

    def test_all_seven_requested_choices_and_extra_option_fields(self):
        samples = {'노출상품ID': '811122', '옵션ID': '911122', '등록상품ID': '711122',
                   '모델명': 'hmp35', '등록상품명': '등록 멀티', '노출상품명': '노출 절전'}
        self.assertEqual(product_search.FIELDS, ('전체', *samples))
        for field, query in samples.items():
            with self.subTest(field=field):
                self.assert_results(query, [1], field)
                self.assert_results(query, [1])
        self.assert_results('화이트', [1])
        self.assert_results('611122', [1])

    def test_selected_field_does_not_match_another_column(self):
        self.assert_results('811122', [], '옵션ID')
        self.assert_results('노출 절전', [], '등록상품명')
        self.assert_results('hmp35', [], '노출상품명')

    def test_numeric_separators_and_partial_ids(self):
        self.assert_results(' 9,111-222 333 ', [1], '옵션ID')
        self.assert_results('222', [1, 2])
        self.assertEqual(product_search.id_term('HMP123456'), 'HMP123456')
        self.assertEqual(product_search.id_term('옵션ID: 9111222333'), '옵션ID: 9111222333')
        self.assert_results('HMP123456', [2])
        self.assert_results('HMP123456', [], '옵션ID')

    def test_percent_underscore_backslash_and_sql_text_are_literal(self):
        for query in ('50%', '%_', r'행사\상품', '_'):
            with self.subTest(query=query):
                self.assert_results(query, [2])
        self.assert_results("' OR 1=1 --", [])

    def test_empty_null_missing_fields_and_no_cross_column_match(self):
        self.assert_results('', [1, 2, 3, 4], '노출상품명')
        self.assert_results('   ', [1, 2, 3, 4])
        self.assert_results('앞 뒤', [])
        self.assert_results('None', [])
        self.assertTrue(product_search.matches({}, None))
        self.assertFalse(product_search.matches({}, '상품'))

    def test_unavailable_field_does_not_reference_missing_columns(self):
        clause, params = product_search.sql_clause('811122', '노출상품ID',
                                                   columns=('model', 'product_name'))
        self.assertEqual((clause, params), ('0', []))
        clause, params = product_search.sql_clause('등록', columns=('product_name', 'invalid; DROP TABLE products'))
        self.assertNotIn('invalid', clause)
        self.assertEqual([row[0] for row in self.conn.execute(
            'SELECT id FROM products p WHERE ' + clause, params)], [1])

    def test_unknown_choice_defaults_to_all_and_invalid_alias_is_rejected(self):
        self.assertEqual(product_search.normalize_field('unrecognized'), '전체')
        self.assert_results('hmp35', [1], 'unrecognized')
        clause, params = product_search.sql_clause('hmp35', alias='')
        self.assertEqual([row[0] for row in self.conn.execute(
            'SELECT id FROM products WHERE ' + clause, params)], [1])
        with self.assertRaises(ValueError):
            product_search.sql_clause('query', alias='p); DROP TABLE products')


if __name__ == '__main__':
    unittest.main()
