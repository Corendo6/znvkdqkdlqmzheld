"""Exercise active-view rendering and selection with synthetic data and no network."""
import sys
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rocket_inventory


class InventoryRenderingTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()
        self.callback_errors = []
        self.root.report_callback_exception = lambda *args: self.callback_errors.append(args)
        self.settings = {}
        self.thumbnails = SimpleNamespace(get=Mock(return_value=None),
                                          drain=Mock(return_value=set()), failed={})
        app = SimpleNamespace(db=SimpleNamespace(
            setting=lambda key, default='': self.settings.get(key, default),
            set_setting=lambda key, value: self.settings.update({key: value})),
            rg_thumbnails=self.thumbnails, open_search_dialog=lambda: None)
        self.rows = [dict(key=str(i), option_id=str(i), product_name=f'상품 {i:03}',
            option_name='화이트 3구', model='HMP35L', image_url=f'https://test.invalid/{i}.png',
            search_text=f'상품 {i:03}', state='품절 임박' if i == 0 else '양호',
            yesterday=1, last7=7, sales_30=30, sales_recorded_days=30,
            stock=4, days=4, pending=0, recommended=18, unit_cost=1000,
            margin=1500, margin_rate=30, gross=dict(sale=5000, cost=1000,
                logistics=2000, margin=1500, margin_rate=30, missing=[])) for i in range(60)]
        with patch.object(rocket_inventory, 'build_rows', return_value=self.rows):
            self.page = rocket_inventory.RocketInventoryPage(self.root, app)
        self.page.pack(fill='both', expand=True)
        self.root.update()

    def tearDown(self):
        if hasattr(self, 'page'):
            self.page.destroy()
        if hasattr(self, 'root'):
            self.root.destroy()
        self.assertEqual(self.callback_errors, [])

    def switch(self, mode):
        self.page.view_mode.set(mode)
        self.page.change_view()
        self.root.update()

    def test_basic_view_does_not_build_hidden_table(self):
        with patch.object(self.page.sheet, 'insert', wraps=self.page.sheet.insert) as insert:
            for _ in range(5):
                self.page.apply_filters()
                self.root.update()
        self.assertEqual(insert.call_count, 0)
        self.assertEqual(self.page.sheet.get_children(), ())
        self.switch('엑셀형 보기')
        self.assertEqual(len(self.page.sheet.get_children()), len(self.page.visible_rows))

    def test_sheet_view_does_not_draw_or_request_hidden_images(self):
        self.switch('엑셀형 보기')
        self.thumbnails.get.reset_mock()
        with patch.object(self.page, '_draw_row', wraps=self.page._draw_row) as draw:
            self.page.apply_filters()
            self.page._draw_viewport()  # A callback already queued before switching is harmless.
            self.page.after_cancel(self.page.after_job)
            self.page._poll_images()
            self.root.update()
        self.assertEqual(draw.call_count, 0)
        self.thumbnails.get.assert_not_called()
        self.assertIsNone(self.page.draw_job)

    def test_view_switch_keeps_selection_urgency_and_latest_filtered_rows(self):
        self.page.toggle_selection('0', True)
        self.switch('엑셀형 보기')
        self.assertEqual(self.page.sheet.selection(), ('0',))
        self.assertIn('urgent', self.page.sheet.item('0', 'tags'))
        self.page.sheet.selection_set('1')
        self.root.update()
        self.assertEqual(self.page.selected, {'1'})
        self.switch('기본 보기')
        self.page.search.set('상품 050')
        self.page.apply_filters()
        self.root.update()
        # Hidden table still holds the previous page; its late selection event must be ignored.
        self.page._sheet_select()
        self.assertEqual(self.page.selected, {'1'})
        self.switch('엑셀형 보기')
        self.assertEqual(len(self.page.sheet.get_children()), 1)
        self.assertEqual(self.page.sheet.item('0', 'values')[1], '상품 050')
        self.assertEqual(self.page.sheet.selection(), ())
        self.page.sheet.selection_set('0')
        self.root.update()
        self.assertEqual(self.page.selected, {'1', '50'})
        self.switch('기본 보기')
        self.assertEqual(self.page._drawn_rows[0]['key'], '50')

    def test_switching_back_redraws_after_empty_filter_and_pending_idle(self):
        self.page._schedule_draw()
        self.switch('엑셀형 보기')
        self.assertIsNone(self.page.draw_job)
        self.page.search.set('no matching product')
        self.page.apply_filters()
        self.root.update()
        self.assertEqual(self.page.sheet.get_children(), ())
        self.switch('기본 보기')
        self.assertEqual(self.page._drawn_rows, {})
        labels = [self.page.canvas.itemcget(i, 'text') for i in self.page.canvas.find_all()
                  if self.page.canvas.type(i) == 'text']
        self.assertIn('조건에 맞는 상품이 없습니다.', labels)


if __name__ == '__main__':
    unittest.main()
