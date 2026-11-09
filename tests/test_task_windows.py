"""Task windows remain independent of selection, page changes and close order."""
import ast
from collections import defaultdict
from pathlib import Path
import tkinter as tk
from tkinter import ttk
from types import MethodType
import unittest
from unittest.mock import Mock
import window_tools
from test_f5_search import SearchApp

SOURCE=Path(__file__).resolve().parents[1]/'coupang_manager.pyw'
nodes=ast.parse(SOURCE.read_text(encoding='utf-8-sig')).body
app=next(n for n in nodes if isinstance(n,ast.ClassDef) and n.name=='CoupangManager')
scope=dict(tk=tk,ttk=ttk,window_tools=window_tools,APP_NAME='Test',money=str,webbrowser=Mock())
exec(compile(ast.Module(body=[next(n for n in nodes if isinstance(n,ast.ClassDef) and n.name=='FieldDialog'),
    next(n for n in app.body if isinstance(n,ast.FunctionDef) and n.name=='show_product_details')],type_ignores=[]),str(SOURCE),'exec'),scope)


class TaskWindowTests(unittest.TestCase):
    def setUp(self):
        self.root=SearchApp()
        self.root.geometry('1000x700+100+100'); self.root.deiconify(); self.root.update()
        self.errors=[]
        self.root.report_callback_exception=lambda *args:self.errors.append(str(args[1]))

    def tearDown(self):
        self.root.destroy()
        self.assertEqual(self.errors,[])

    def test_two_forms_save_out_of_order_without_locking_main_window(self):
        saved=[]
        dialogs=[scope['FieldDialog'](self.root,'Test',[('value','Value')],{'value':str(i)}) for i in range(2)]
        for d in dialogs:
            window_tools.when_closed(d,lambda d=d:saved.append(d.result['value']))
        main_action=Mock(); ttk.Button(self.root,command=main_action).invoke()
        self.assertIsNone(self.root.grab_current()); main_action.assert_called_once()
        dialogs[0].save(); self.root.update()
        self.assertEqual(saved,['0']); self.assertTrue(dialogs[1].winfo_exists())
        dialogs[1].save(); self.root.update()
        self.assertEqual(saved,['0','1'])

    def test_multiple_search_windows_keep_their_page_and_inputs(self):
        self.root.open_search_dialog(); first=self.root._search_dialog
        first.search_var.set('first')
        self.root.current_page='sales'; self.root.open_search_dialog(); second=self.root._search_dialog
        self.assertIsNot(first,second); self.assertIsNone(self.root.grab_current())
        first.event_generate('<Return>'); self.root.update()
        # Invoke the button directly: keyboard focus need not be on the older window.
        if first.winfo_exists():
            self.button(first,'검색 (Enter)').invoke(); self.root.update()
        self.assertEqual(self.root.queries['products'].get(),'first')
        self.assertTrue(second.winfo_exists())
        second.search_var.set('second'); self.button(second,'검색 (Enter)').invoke(); self.root.update()
        self.assertEqual(self.root.queries['sales'].get(),'second')

    def button(self,win,text):
        todo=list(win.winfo_children())
        while todo:
            w=todo.pop()
            if isinstance(w,ttk.Button) and w.cget('text')==text:return w
            todo.extend(w.winfo_children())
        self.fail(text)

    def test_details_keep_original_option_for_links_and_history(self):
        selected=[defaultdict(str,id=1,option_id='101',product_name='First')]
        self.root.selected_product=lambda:selected[0]
        self.root._resolved_product_link=lambda r:'https://example.test/'+r['option_id']
        self.root._clipboard_set=Mock(); self.root.show_exposure_history=Mock()
        method=MethodType(scope['show_product_details'],self.root)
        method(); first=self.root._product_detail_dialog
        selected[0]=defaultdict(str,id=2,option_id='102',product_name='Second')
        method(); second=self.root._product_detail_dialog
        self.button(first,'쿠팡 상품 열기').invoke()
        scope['webbrowser'].open_new_tab.assert_called_with('https://example.test/101')
        self.button(first,'노출ID 이력').invoke()
        self.root.show_exposure_history.assert_called_once_with([1])
        self.assertTrue(first.winfo_exists()); self.assertTrue(second.winfo_exists())
        self.assertIsNone(self.root.grab_current())

    def test_position_stays_near_parent(self):
        win=tk.Toplevel(self.root); win.geometry('300x200')
        window_tools.place(win,self.root); self.root.update()
        self.assertLess(abs((win.winfo_rootx()+150)-(self.root.winfo_rootx()+500)),80)
        self.assertLess(abs((win.winfo_rooty()+100)-(self.root.winfo_rooty()+350)),80)


class MonitorGeometryTests(unittest.TestCase):
    def test_left_monitor_negative_coordinates_are_preserved(self):
        x,y=window_tools.centered_rect((-1800,100,1200,800),(500,400),(-1920,0,0,1040))
        self.assertEqual((x,y),(-1450,300))

    def test_above_monitor_and_edges_are_clamped_to_work_area(self):
        self.assertEqual(window_tools.centered_rect((20,-1000,800,700),(1000,900),(0,-1080,1920,0)),(0,-1080))


if __name__=='__main__':unittest.main()
