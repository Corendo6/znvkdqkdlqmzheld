"""The real F5 dialog passes the selected field to each page's local search."""
import ast
from pathlib import Path
import sys
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import product_search

SOURCE = Path(__file__).resolve().parents[1] / 'coupang_manager.pyw'
APP = next(n for n in ast.parse(SOURCE.read_text(encoding='utf-8-sig')).body
           if isinstance(n, ast.ClassDef) and n.name == 'CoupangManager')
NAMES = {'open_search_dialog', '_search_field', '_set_search_field'}
scope = dict(tk=tk, ttk=ttk, product_search=product_search)
exec(compile(ast.Module(body=[n for n in APP.body if isinstance(n, ast.FunctionDef)
                             and n.name in NAMES], type_ignores=[]), str(SOURCE), 'exec'), scope)


class SearchApp(tk.Tk):
    open_search_dialog = scope['open_search_dialog']
    _search_field = scope['_search_field']
    _set_search_field = scope['_set_search_field']

    def __init__(self):
        super().__init__()
        self.withdraw()
        self.current_page = 'products'
        self._search_dialog = None
        self._search_fields = {}
        self.queries = {page: tk.StringVar(self) for page in
                        ('products', 'rocket_inventory', 'master', 'margin', 'sales', 'search')}
        self.refreshes = {page: Mock() for page in self.queries}
        self.master_tabs = Mock()
        self.master_models_tab = object()
        self.gross_margin_notebook = Mock()

    def _search_target(self, page):
        return self.queries[page], self.refreshes[page]

    def show_page(self, page):
        self.current_page = page


class F5SearchTests(unittest.TestCase):
    def setUp(self):
        try:
            self.app = SearchApp()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.addCleanup(self.app.destroy)
        self.errors = []
        self.app.report_callback_exception = lambda *args: self.errors.append(args)

    def button(self, dialog, label):
        widgets = list(dialog.winfo_children())
        while widgets:
            widget = widgets.pop()
            if isinstance(widget, ttk.Button) and widget.cget('text') == label:
                return widget
            widgets.extend(widget.winfo_children())
        self.fail('Missing dialog button: ' + label)

    def apply(self, query, field):
        self.app.open_search_dialog()
        dialog = self.app._search_dialog
        self.assertEqual(tuple(dialog.field_combo.cget('values')), product_search.FIELDS)
        dialog.search_field.set(field)
        dialog.search_var.set(query)
        self.button(dialog, '검색 (Enter)').invoke()
        self.app.update()
        self.assertEqual(self.errors, [])

    def test_every_page_applies_selected_field_and_query(self):
        for page in self.app.queries:
            for field in product_search.FIELDS:
                with self.subTest(page=page, field=field):
                    self.app.current_page = page
                    self.apply('  검색어  ', field)
                    self.assertEqual(self.app._search_field(page), field)
                    self.assertEqual(self.app.queries[page].get(), '검색어')
                    self.assertIsNone(self.app._search_dialog)
                    self.app.refreshes[page].assert_called()

    def test_cancel_does_not_change_query_or_field_and_reopen_remembers(self):
        self.apply('기존', '모델명')
        self.app.open_search_dialog()
        dialog = self.app._search_dialog
        self.assertEqual(dialog.search_field.get(), '모델명')
        self.assertEqual(dialog.search_var.get(), '기존')
        dialog.search_field.set('옵션ID')
        dialog.search_var.set('변경')
        self.button(dialog, '닫기 (Esc)').invoke()
        self.assertEqual(self.app._search_field('products'), '모델명')
        self.assertEqual(self.app.queries['products'].get(), '기존')

    def test_blank_search_clears_scope_and_dashboard_opens_product_search(self):
        self.apply('검색', '노출상품ID')
        self.apply('  ', '노출상품ID')
        self.assertEqual(self.app._search_field('products'), '전체')
        self.assertEqual(self.app.queries['products'].get(), '')
        self.app.current_page = 'product_dashboard'
        self.apply('12345', '등록상품ID')
        self.assertEqual(self.app.current_page, 'products')
        self.assertEqual(self.app.queries['products'].get(), '12345')
        self.assertEqual(self.app._search_field('products'), '등록상품ID')


if __name__ == '__main__':
    unittest.main()
