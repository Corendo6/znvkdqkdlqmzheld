"""F5 inventory fields share the same filters in the basic and spreadsheet views."""
import unittest
import test_inventory_performance as inventory_tests


class InventorySearchTests(unittest.TestCase):
    tearDown = inventory_tests.InventoryRenderingTests.tearDown
    switch = inventory_tests.InventoryRenderingTests.switch

    def setUp(self):
        inventory_tests.InventoryRenderingTests.setUp(self)
        self.field = '전체'
        self.page.app._search_field = lambda page: self.field
        self.page.app._set_search_field = lambda page, value: setattr(self, 'field', value)
        self.rows[0].update(exposed_product_id='9900112233', option_id='8800112233',
            registered_product_id='7700112233', model='UNIQUE-MODEL',
            product_name='등록 한정 상품', display_name='노출 특별 상품')
        self.rows[1].update(model='노출 특별 상품', product_name='다른 등록 상품',
                            display_name='다른 노출 상품')
        self.page.apply_filters()
        self.root.update()

    def find(self, field, query):
        self.field = field
        self.page.search.set(query)
        self.page.apply_filters()
        self.root.update()
        return {r['key'] for r in self.page.filtered_rows}

    def test_each_identity_field_and_all_work_in_both_views(self):
        cases = [('노출상품ID', '9900112233'), ('옵션ID', '8800112233'),
                 ('등록상품ID', '7700112233'), ('모델명', 'unique-model'),
                 ('등록상품명', '등록 한정'), ('노출상품명', '노출 특별')]
        for view in ('기본 보기', '엑셀형 보기'):
            self.switch(view)
            for field, query in cases:
                with self.subTest(view=view, field=field):
                    self.assertEqual(self.find(field, query), {'0'})
                    self.assertIn(field, self.page.search_label.cget('text'))
                    self.assertIn('0', self.find('전체', query))
                    if view == '엑셀형 보기':
                        self.assertEqual(len(self.page.sheet.get_children()), len(self.page.visible_rows))

    def test_selected_field_does_not_match_other_fields_and_id_separators_work(self):
        self.assertEqual(self.find('노출상품명', '노출 특별'), {'0'})
        self.assertEqual(self.find('모델명', '노출 특별'), {'1'})
        self.assertEqual(self.find('옵션ID', '8800-112-233'), {'0'})
        self.assertEqual(self.find('노출상품ID', '8800112233'), set())

    def test_search_keeps_state_filter_and_clear_restores_all_field(self):
        self.page.filter.set('품절 임박')
        self.assertEqual(self.find('모델명', '노출 특별'), set())
        self.assertEqual(self.find('노출상품명', '노출 특별'), {'0'})
        self.page.clear_search()
        self.root.update()
        self.assertEqual(self.field, '전체')
        self.assertEqual(self.page.search.get(), '')
        self.assertEqual(self.page.filter.get(), '품절 임박')
        self.assertEqual({r['key'] for r in self.page.filtered_rows}, {'0'})


if __name__ == '__main__':
    unittest.main()
