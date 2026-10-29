"""Collection regressions for split orders and independent manual sales."""
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_daily_sales import MemoryDB, TODAY, VENDOR
import rocket_sales


class OrderCollectionTests(unittest.TestCase):
    def setUp(self):
        self.db = MemoryDB()
        self.conn = self.db.conn
        self.addCleanup(self.conn.close)
        clock = patch.object(rocket_sales, 'today_kst', return_value=TODAY)
        clock.start()
        self.addCleanup(clock.stop)
        self.first = (TODAY - timedelta(days=30)).isoformat()
        self.last = (TODAY - timedelta(days=1)).isoformat()

    def item(self, option='101', quantity=3, price=1000, name='Product'):
        return dict(currency='KRW', vendorItemId=option, salesQuantity=quantity,
                    unitSalesPrice=price, productName=name)

    def order(self, *items, order_id='20001', days_ago=1):
        paid = datetime.combine(TODAY - timedelta(days=days_ago), datetime.min.time(),
                                tzinfo=rocket_sales.KST) + timedelta(hours=12)
        return dict(vendorId=VENDOR, orderId=order_id, paidAt=int(paid.timestamp() * 1000),
                    orderItems=list(items))

    def api(self, *pages, detail=None):
        replies = iter([dict(code=200, data=orders, nextToken=str(i + 1) if i + 1 < len(pages) else None)
                        for i, orders in enumerate(pages)])
        self.requests = []
        def request(method, path, params=None):
            self.requests.append((method, path))
            if path.endswith('/rg/orders'):
                return next(replies)
            self.assertTrue(path.endswith('/rg/order/20001'), path)
            if isinstance(detail, Exception):
                raise detail
            return dict(code=200, data=detail)
        return SimpleNamespace(vendor_id=VENDOR, check_cancelled=lambda: None,
                               _request=request)

    def fetch_save(self, *pages, detail=None):
        orders = rocket_sales.fetch_orders(self.api(*pages, detail=detail), self.first, self.last)
        result = rocket_sales.save_orders(self.conn, VENDOR, orders, self.first, self.last)
        return orders, result

    def quantities(self):
        return {r['option_id']: r['quantity'] for r in self.conn.execute('SELECT * FROM rg_order_items')}

    def add_manual(self, model='OTHER', option=''):
        self.conn.execute('''INSERT INTO sales(sale_date,model,method,revenue,orders,quantity,option_id,source)
            VALUES (?,?,?,?,?,?,?,?)''', (self.last, model, '로켓그로스', 900, 1, 1, option, 'manual'))
        self.conn.commit()

    def api_sales(self):
        return self.conn.execute('SELECT * FROM sales WHERE source=?',
                                 (rocket_sales.SOURCE_PREFIX + VENDOR,)).fetchall()

    def scripted_api(self, replies):
        replies = iter(replies)
        requests = []
        def request(method, path, params=None):
            requests.append((method, path, dict(params) if params is not None else None))
            return next(replies)
        api = SimpleNamespace(vendor_id=VENDOR, check_cancelled=Mock(),
                               _request=request, wait=Mock(), progress=Mock(), requests=requests)
        return api

    def collect_and_save(self, api):
        orders = rocket_sales.fetch_orders(api, self.first, self.last)
        return rocket_sales.save_orders(self.conn, VENDOR, orders, self.first, self.last)

    def storage_snapshot(self):
        return {name: [tuple(r) for r in self.conn.execute('SELECT * FROM ' + name)]
                for name in ('rg_order_items', 'sales', 'rg_sales_coverage', 'settings')}

    def test_transient_list_errors_retry_exact_next_token_and_dates(self):
        api = self.scripted_api([
            dict(code=200, data=[self.order(self.item('101', 2))], nextToken='opaque+token/=='),
            dict(code=429, message='Too Many Requests'),
            dict(code=503, message='Temporary failure'),
            dict(code=200, data=[self.order(self.item('102', 4), order_id='20002')])])
        self.collect_and_save(api)
        self.assertEqual(api.wait.call_args_list, [call(30), call(60)])
        self.assertEqual(api.requests[1][2], api.requests[2][2])
        self.assertEqual(api.requests[1][2], api.requests[3][2])
        self.assertEqual(api.requests[1][2]['nextToken'], 'opaque+token/==')
        self.assertEqual(api.requests[1][2]['paidDateFrom'], self.first.replace('-', ''))
        self.assertEqual(api.requests[1][2]['paidDateTo'], self.last.replace('-', ''))
        self.assertEqual(self.quantities(), {'101': 2, '102': 4})

    def test_json_500_with_rate_limit_message_retries_detail_then_saves(self):
        fragment = self.order(self.item(quantity=1))
        api = self.scripted_api([
            dict(code=200, data=[fragment, fragment]),
            dict(code=500, message='429 Too Many Requests'),
            dict(code=200, data=self.order(self.item(quantity=2)))])
        self.collect_and_save(api)
        api.wait.assert_called_once_with(30)
        self.assertEqual(api.requests[1], api.requests[2])
        self.assertTrue(api.requests[1][1].endswith('/rg/order/20001'))
        self.assertIn('호출 제한', api.progress.call_args.args[0])
        self.assertEqual(self.quantities(), {'101': 2})

    def test_exhausted_list_retries_leave_all_saved_state_unchanged(self):
        self.fetch_save([self.order(self.item('999', 2), order_id='90000')])
        before = self.storage_snapshot()
        api = self.scripted_api([
            dict(code=200, data=[self.order(self.item())], nextToken='next'),
            dict(code=502, message='Bad Gateway'),
            dict(code=503, message='Unavailable'),
            dict(code=504, message='Timeout')])
        with self.assertRaises(ValueError):
            self.collect_and_save(api)
        self.assertEqual(api.wait.call_args_list, [call(30), call(60)])
        self.assertEqual(len(api.requests), 4)
        self.assertEqual(self.storage_snapshot(), before)

    def test_exhausted_detail_retries_leave_all_saved_state_unchanged(self):
        self.fetch_save([self.order(self.item('999', 2), order_id='90000')])
        before = self.storage_snapshot()
        fragment = self.order(self.item(quantity=1))
        api = self.scripted_api([dict(code=200, data=[fragment, fragment]),
                                *[dict(code=500, message='429 Too Many Requests')] * 3])
        with self.assertRaises(ValueError):
            self.collect_and_save(api)
        self.assertEqual(api.wait.call_args_list, [call(30), call(60)])
        self.assertEqual(len(api.requests), 4)
        self.assertEqual(self.storage_snapshot(), before)

    def test_nontransient_list_error_is_not_retried(self):
        api = self.scripted_api([dict(code=400, message='Invalid query')])
        with self.assertRaises(ValueError):
            self.collect_and_save(api)
        api.wait.assert_not_called()
        self.assertEqual(len(api.requests), 1)
        self.assertEqual(self.quantities(), {})

    def test_nontransient_detail_error_is_not_retried(self):
        fragment = self.order(self.item(quantity=1))
        api = self.scripted_api([dict(code=200, data=[fragment, fragment]),
                                dict(code=403, message='Forbidden')])
        with self.assertRaises(ValueError):
            self.collect_and_save(api)
        api.wait.assert_not_called()
        self.assertEqual(len(api.requests), 2)
        self.assertEqual(self.quantities(), {})

    def test_cancel_during_retry_wait_stops_requests_without_saving(self):
        self.fetch_save([self.order(self.item('999', 2), order_id='90000')])
        before = self.storage_snapshot()
        api = self.scripted_api([dict(code=429, message='Too Many Requests')])
        api.wait.side_effect = RuntimeError('cancel requested')
        with self.assertRaisesRegex(RuntimeError, 'cancel requested'):
            self.collect_and_save(api)
        self.assertEqual(len(api.requests), 1)
        self.assertEqual(self.storage_snapshot(), before)

    def test_split_order_within_one_page_preserves_all_options(self):
        orders, result = self.fetch_save([
            self.order(self.item('101', 3)), self.order(self.item('102', 5))],
            detail=self.order(self.item('101', 3), self.item('102', 5)))
        self.assertEqual(len(orders), 1)
        self.assertEqual(result['orders'], 1)
        self.assertEqual(self.quantities(), {'101': 3, '102': 5})
        self.assertEqual(sum('/rg/order/' in path for _, path in self.requests), 0)

    def test_split_order_across_pages_preserves_all_options(self):
        orders, _ = self.fetch_save([self.order(self.item('101', 3))],
                                    [self.order(self.item('102', 5))],
                                    detail=self.order(self.item('101', 3), self.item('102', 5)))
        self.assertEqual(set(orders[0]['items']), {'101', '102'})
        self.assertEqual(self.quantities(), {'101': 3, '102': 5})
        self.assertEqual(sum('/rg/order/' in path for _, path in self.requests), 0)

    def test_overlap_against_accumulated_union_requires_one_detail(self):
        self.fetch_save([self.order(self.item('101', 1))],
                        [self.order(self.item('102', 5))],
                        [self.order(self.item('101', 1))],
                        detail=self.order(self.item('101', 2), self.item('102', 5)))
        self.assertEqual(self.quantities(), {'101': 2, '102': 5})
        self.assertEqual(sum('/rg/order/' in path for _, path in self.requests), 1)

    def test_differing_paid_days_with_disjoint_options_requires_detail(self):
        orders, _ = self.fetch_save([self.order(self.item('101', 3), days_ago=1)],
                                    [self.order(self.item('102', 5), days_ago=2)],
                                    detail=self.order(self.item('101', 3), self.item('102', 5), days_ago=2))
        self.assertEqual(self.quantities(), {'101': 3, '102': 5})
        self.assertEqual(orders[0]['paid_date'], (TODAY - timedelta(days=2)).isoformat())
        self.assertEqual(sum('/rg/order/' in path for _, path in self.requests), 1)

    def test_identical_repeated_order_does_not_double_count(self):
        order = self.order(self.item('101', 3), self.item('102', 5))
        self.fetch_save([order], [order], detail=order)
        self.assertEqual(self.quantities(), {'101': 3, '102': 5})
        self.assertEqual(sum(r['orders'] for r in self.api_sales()), 2)
        self.assertEqual(sum(r['quantity'] for r in self.api_sales()), 8)

    def test_overlapping_subset_then_superset_does_not_double_count(self):
        self.fetch_save([self.order(self.item('101', 3))],
                        [self.order(self.item('101', 3), self.item('102', 5))],
                        detail=self.order(self.item('101', 3), self.item('102', 5)))
        self.assertEqual(self.quantities(), {'101': 3, '102': 5})

    def test_overlapping_superset_then_subset_does_not_drop_options(self):
        self.fetch_save([self.order(self.item('101', 3), self.item('102', 5))],
                        [self.order(self.item('101', 3))],
                        detail=self.order(self.item('101', 3), self.item('102', 5)))
        self.assertEqual(self.quantities(), {'101': 3, '102': 5})

    def test_name_uses_authoritative_detail(self):
        orders, _ = self.fetch_save([self.order(self.item(name='Old name'))],
                                    [self.order(self.item(name='List name'))],
                                    [self.order(self.item(name=''))],
                                    detail=self.order(self.item(name='Canonical name')))
        self.assertEqual(orders[0]['items']['101']['product_name'], 'Canonical name')
        row = self.conn.execute('SELECT * FROM rg_order_items').fetchone()
        self.assertEqual(row['product_name'], 'Canonical name')
        self.assertEqual(row['quantity'], 3)
        self.assertEqual(sum('/rg/order/' in path for _, path in self.requests), 1)

    def test_equal_list_quantities_can_be_separate_detail_items(self):
        fragment = self.order(self.item(quantity=1))
        self.fetch_save([fragment], [fragment],
                        detail=self.order(self.item(quantity=1), self.item(quantity=1)))
        self.assertEqual(self.quantities(), {'101': 2})
        self.assertEqual(self.api_sales()[0]['revenue'], 2000)

    def test_conflicting_list_quantities_resolve_to_canonical_total(self):
        self.fetch_save([self.order(self.item(quantity=2))], [self.order(self.item(quantity=1))],
                        detail=self.order(self.item(quantity=1), self.item(quantity=1), self.item(quantity=1)))
        self.assertEqual(self.quantities(), {'101': 3})
        self.assertEqual(self.api_sales()[0]['revenue'], 3000)

    def test_singleton_detail_list_is_accepted(self):
        fragment = self.order(self.item(quantity=1))
        self.fetch_save([fragment], [fragment], detail=[self.order(self.item(quantity=2))])
        self.assertEqual(self.quantities(), {'101': 2})

    def test_documented_iso_date_and_sales_price_detail_is_accepted(self):
        fragment = self.order(self.item(quantity=1))
        detail = self.order(self.item(quantity=2))
        detail['paidAt'] = self.last + 'T12:00:00+09:00'
        detail['orderItems'][0]['salesPrice'] = detail['orderItems'][0].pop('unitSalesPrice')
        self.fetch_save([fragment], [fragment], detail=detail)
        self.assertEqual(self.quantities(), {'101': 2})
        self.assertEqual(self.api_sales()[0]['revenue'], 2000)

    def test_unique_order_requires_no_detail_request(self):
        self.fetch_save([self.order(self.item())])
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(self.requests[0][1].endswith('/rg/orders'))

    def assert_rejected_without_partial_save(self, detail, error=ValueError):
        self.fetch_save([self.order(self.item('999', 2), order_id='90000')])
        before = {name: [tuple(r) for r in self.conn.execute('SELECT * FROM ' + name)]
                  for name in ('rg_order_items', 'sales', 'rg_sales_coverage', 'settings')}
        with self.assertRaises(error):
            self.fetch_save([self.order(self.item(quantity=2))],
                            [self.order(self.item(quantity=1))], detail=detail)
        after = {name: [tuple(r) for r in self.conn.execute('SELECT * FROM ' + name)]
                 for name in before}
        self.assertEqual(after, before)

    def test_missing_detail_rejects_without_partial_save(self):
        self.assert_rejected_without_partial_save(None)

    def test_malformed_detail_rejects_without_partial_save(self):
        self.assert_rejected_without_partial_save('not-an-order')

    def test_wrong_detail_order_rejects_without_partial_save(self):
        self.assert_rejected_without_partial_save(self.order(self.item(), order_id='88888'))

    def test_wrong_detail_vendor_rejects_without_partial_save(self):
        detail = self.order(self.item())
        detail['vendorId'] = 'OTHER-VENDOR'
        self.assert_rejected_without_partial_save(detail)

    def test_out_of_window_detail_rejects_without_partial_save(self):
        self.assert_rejected_without_partial_save(self.order(self.item(), days_ago=31))

    def test_in_window_canonical_paid_date_is_authoritative(self):
        fragment = self.order(self.item(quantity=1))
        orders, _ = self.fetch_save([fragment], [fragment],
                                    detail=self.order(self.item(quantity=3), days_ago=2))
        expected = (TODAY - timedelta(days=2)).isoformat()
        self.assertEqual(orders[0]['paid_date'], expected)
        self.assertEqual(self.api_sales()[0]['sale_date'], expected)
        metrics = rocket_sales.daily_quantities(self.conn, VENDOR, today=TODAY)['by_option']['101']
        self.assertEqual(metrics['yesterday'], 0)
        self.assertEqual(metrics['last7'], 3)
        self.assertEqual(metrics['sales_30'], 3)

    def test_missing_previously_observed_option_rejects_without_partial_save(self):
        self.assert_rejected_without_partial_save(self.order(self.item('102')))

    def test_detail_network_error_rejects_without_partial_save(self):
        self.assert_rejected_without_partial_save(RuntimeError('network unavailable'), error=RuntimeError)

    def test_unrelated_model_sale_does_not_suppress_unmapped_api_option(self):
        self.add_manual(model='UNRELATED')
        _, result = self.fetch_save([self.order(self.item())])
        self.assertEqual(result['inserted'], 1)
        self.assertEqual(result['conflicts'], 0)
        self.assertEqual(result['unmapped'], 1)
        self.assertEqual(len(self.api_sales()), 1)
        self.assertIsNotNone(self.conn.execute('SELECT 1 FROM unmatched_options WHERE option_id=?', ('101',)).fetchone())

    def test_same_known_model_manual_override_still_takes_precedence(self):
        self.conn.execute('INSERT INTO option_model_map VALUES (?,?)', ('101', 'MODEL'))
        self.conn.commit()
        self.add_manual(model='MODEL')
        _, result = self.fetch_save([self.order(self.item())])
        self.assertEqual(result['conflicts'], 1)
        self.assertEqual(self.api_sales(), [])
        self.assertEqual(self.quantities(), {'101': 3})

    def test_exact_option_override_still_takes_precedence_when_unmapped(self):
        self.add_manual(model='', option='101')
        _, result = self.fetch_save([self.order(self.item())])
        self.assertEqual(result['conflicts'], 1)
        self.assertEqual(self.api_sales(), [])
        self.assertEqual(self.quantities(), {'101': 3})

    def test_prefer_external_model_preserves_unknown_and_unrelated_api_rows(self):
        self.conn.executemany('INSERT INTO option_model_map VALUES (?,?)', [('102', 'MATCH'), ('103', 'OTHER')])
        self.conn.commit()
        self.fetch_save([self.order(self.item('101'), self.item('102'), self.item('103'))])
        rocket_sales.prefer_external_sale(self.conn, self.last, 'MATCH', '로켓그로스')
        self.assertEqual({r['option_id'] for r in self.api_sales()}, {'101', '103'})
        self.assertEqual(set(self.quantities()), {'101', '102', '103'})

    def test_prefer_external_exact_option_preserves_other_unknown_rows(self):
        self.fetch_save([self.order(self.item('101'), self.item('102'))])
        rocket_sales.prefer_external_sale(self.conn, self.last, '', '로켓그로스', option_id='101')
        self.assertEqual({r['option_id'] for r in self.api_sales()}, {'102'})
        self.assertEqual(set(self.quantities()), {'101', '102'})


if __name__ == '__main__':
    unittest.main()
