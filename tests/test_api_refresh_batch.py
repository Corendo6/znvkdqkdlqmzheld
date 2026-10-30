"""API progress must not trigger duplicate redraws or hide partial saves."""
import ast
from pathlib import Path
import queue
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock


source = Path(__file__).resolve().parents[1] / 'coupang_manager.pyw'
app_class = next(node for node in ast.parse(source.read_text(encoding='utf-8-sig')).body
                 if isinstance(node, ast.ClassDef) and node.name == 'CoupangManager')
method = next(node for node in app_class.body
              if isinstance(node, ast.FunctionDef) and node.name == '_poll_api_queue')
namespace = dict(queue=queue, LOGGER=Mock(), tk=SimpleNamespace(TclError=RuntimeError))
exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), 'exec'), namespace)


class APIRefreshBatchTests(unittest.TestCase):
    def setUp(self):
        self.app = SimpleNamespace(api_queue=queue.Queue(), api_busy=True,
            _handle_api_message=Mock(), _update_api_status=Mock(), after=Mock())
        self.app._poll_api_queue = MethodType(namespace['_poll_api_queue'], self.app)

    def poll(self, *messages):
        for msg in messages:
            self.app.api_queue.put(msg)
        self.app._poll_api_queue()
        return [call.args[0] for call in self.app._handle_api_message.call_args_list]

    def test_final_refresh_replaces_saved_and_progress_redraws(self):
        complete = ('sync_complete', {'orders': {'ok': True}}, False)
        self.assertEqual(self.poll(('status', 'fetch'), ('inventory_saved', 'stamp'),
            ('orders_saved',), ('status', 'done'), complete), [complete])

    def test_partial_inventory_is_delivered_while_orders_are_still_fetching(self):
        messages = [('status', 'inventory'), ('inventory_saved', 'stamp'),
                    ('status', 'orders page 1'), ('status', 'orders page 2')]
        self.assertEqual(self.poll(*messages), [messages[1], messages[-1]])

    def test_saved_orders_include_inventory_refresh_without_waiting_for_products(self):
        messages = [('inventory_saved', 'stamp'), ('orders_saved',), ('status', 'products')]
        self.assertEqual(self.poll(*messages), messages[1:])

    def test_poll_is_bounded_and_keeps_completion_for_next_tick(self):
        complete = ('sync_complete', {}, False)
        self.assertEqual(self.poll(*[('status', str(i)) for i in range(50)], complete),
                         [('status', '49')])
        self.assertEqual(self.app.api_queue.qsize(), 1)
        self.app._handle_api_message.reset_mock()
        self.assertEqual(self.poll(), [complete])
        self.app.after.assert_called_with(200, self.app._poll_api_queue)

    def test_handler_failure_does_not_stop_later_messages_or_polling(self):
        self.app._handle_api_message.side_effect = [ValueError('render error'), None]
        messages = [('inventory_saved', 'stamp'), ('status', 'more')]
        self.assertEqual(self.poll(*messages), messages)
        self.app.after.assert_called_once_with(200, self.app._poll_api_queue)


if __name__ == '__main__':
    unittest.main()
