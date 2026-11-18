"""Shared retry budgets and run-scoped history pages, with no live requests."""
import ast
from datetime import date, datetime, timedelta
import hashlib
import hmac
import io
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api_query
import revenue_exposure


source = Path(__file__).resolve().parents[1] / 'coupang_manager.pyw'
classes = [node for node in ast.parse(source.read_text(encoding='utf-8-sig')).body
           if isinstance(node, ast.ClassDef)
           and node.name in ('CoupangAPI', 'CoupangAPIError', 'CoupangAPICancelled')]
namespace = dict(api_query=api_query, revenue_exposure=revenue_exposure, threading=threading, time=time, urllib=urllib,
                 json=json, datetime=datetime, date=date, timedelta=timedelta,
                 hmac=hmac, hashlib=hashlib, LOGGER=Mock())
exec(compile(ast.Module(body=classes, type_ignores=[]), str(source), 'exec'), namespace)
CoupangAPI = namespace['CoupangAPI']
CoupangAPIError = namespace['CoupangAPIError']
CoupangAPICancelled = namespace['CoupangAPICancelled']
ORDERS = '/v2/providers/rg_open_api/apis/api/v1/vendors/A1/rg/orders'
INVENTORY = '/v2/providers/rg_open_api/apis/api/v1/vendors/A1/rg/inventory/summaries'


def response(payload):
    return io.BytesIO(json.dumps(payload).encode('utf-8'))


def http_error(code, body='temporary error'):
    return urllib.error.HTTPError('https://example.invalid', code, 'error', {},
                                  io.BytesIO(body.encode('utf-8')))


class RequestPolicyTests(unittest.TestCase):
    def setUp(self):
        self.api = CoupangAPI('A1', 'not-a-key', 'not-a-secret')
        self.api.wait = Mock()
        self.api._authorization = Mock(return_value='test signature')
        self.api.progress = Mock()
        CoupangAPI._rg_next_at = 0

    def run_requests(self, path, replies):
        with patch.object(urllib.request, 'urlopen', side_effect=replies) as opened:
            result = self.api._request('GET', path, {'nextToken': 'opaque+/=='})
        return result, opened

    def test_http_and_json_failures_share_three_wire_attempts(self):
        result, opened = self.run_requests(ORDERS, [http_error(503),
            response({'code': 500}), response({'code': 200, 'data': []})])
        self.assertEqual(result['code'], 200)
        self.assertEqual(opened.call_count, 3)
        self.assertEqual(self.api._authorization.call_count, 3)
        self.assertIn(call(1.8), self.api.wait.call_args_list)
        self.assertIn(call(60), self.api.wait.call_args_list)
        urls = {args.args[0].full_url for args in opened.call_args_list}
        self.assertEqual(len(urls), 1)
        self.assertIn('nextToken=opaque%2B%2F%3D%3D', urls.pop())

    def test_exhausted_mixed_errors_do_not_restart_http_budget(self):
        with patch.object(urllib.request, 'urlopen', side_effect=[http_error(503),
                response({'code': 500}), http_error(503)]) as opened:
            with self.assertRaises(CoupangAPIError) as caught:
                api_query.request_json(self.api, 'GET', ORDERS)
        self.assertTrue(caught.exception.stop_sync)
        self.assertEqual(opened.call_count, 3)

    def test_inventory_json_retry_matches_order_policy(self):
        result, opened = self.run_requests(INVENTORY, [response({'code': 429}),
            response({'code': 503}), response({'code': 'SUCCESS', 'data': []})])
        self.assertEqual(opened.call_count, 3)
        self.assertEqual(result['data'], [])
        self.assertIn(call(30), self.api.wait.call_args_list)
        self.assertIn(call(60), self.api.wait.call_args_list)

    def test_auth_error_stops_immediately(self):
        with patch.object(urllib.request, 'urlopen', side_effect=http_error(403)) as opened:
            with self.assertRaises(CoupangAPIError) as caught:
                self.api._request('GET', ORDERS)
        self.assertTrue(caught.exception.stop_sync)
        self.assertEqual(opened.call_count, 1)

    def test_normal_400_is_not_retried(self):
        with patch.object(urllib.request, 'urlopen', side_effect=http_error(400)) as opened:
            with self.assertRaises(CoupangAPIError):
                self.api.get_product('100')
        self.assertEqual(opened.call_count, 1)

    def test_only_generic_default_400_uses_detail_three_attempt_budget(self):
        body = '{"code":"DEFAULT","message":"자세한 사항은 로그를 확인해주세요"}'
        with patch.object(urllib.request, 'urlopen', side_effect=[http_error(400, body),
                http_error(400, body), response({'code': 'SUCCESS', 'data': {'id': '100'}})]) as opened:
            self.assertEqual(self.api.get_product('100'), {'id': '100'})
        self.assertEqual(opened.call_count, 3)
        self.assertEqual(self.api.wait.call_args_list, [call(1.8), call(3.6)])

    def test_regular_network_failure_uses_two_attempts_and_public_error(self):
        with patch.object(urllib.request, 'urlopen', side_effect=TimeoutError('slow')) as opened:
            with self.assertRaises(CoupangAPIError) as caught:
                self.api._request('GET', self.api.PRODUCT_LIST_PATH)
        self.assertEqual(opened.call_count, 2)
        self.assertTrue(caught.exception.stop_sync)
        self.api.wait.assert_called_once_with(1.0)

    def test_malformed_json_does_not_retry(self):
        with patch.object(urllib.request, 'urlopen', return_value=io.BytesIO(b'{bad')) as opened:
            with self.assertRaises(CoupangAPIError):
                self.api._request('GET', ORDERS)
        self.assertEqual(opened.call_count, 1)

    def test_cancel_during_retry_stops_before_next_wire_attempt(self):
        def wait(seconds):
            if seconds == 30:
                raise CoupangAPICancelled()
        self.api.wait.side_effect = wait
        with patch.object(urllib.request, 'urlopen', return_value=response({'code': 429})) as opened:
            with self.assertRaises(CoupangAPICancelled):
                self.api._request('GET', ORDERS)
        self.assertEqual(opened.call_count, 1)

    def test_adapter_supports_simple_existing_clients_without_nested_retry(self):
        fake = SimpleNamespace(check_cancelled=Mock(), wait=Mock(), progress=Mock(),
                               _request=Mock(side_effect=[{'code': 429}, {'code': 500}, {'code': 200}]))
        self.assertEqual(api_query.request_json(fake, 'GET', ORDERS, {'nextToken': 'x'}), {'code': 200})
        self.assertEqual(fake.wait.call_args_list, [call(30), call(60)])
        self.assertEqual(fake._request.call_args_list, [call('GET', ORDERS, {'nextToken': 'x'})] * 3)

    def test_orders_and_inventory_are_fetched_again_for_every_explicit_read(self):
        replies = [response({'code': 200, 'data': []}) for _ in range(4)]
        with patch.object(urllib.request, 'urlopen', side_effect=replies) as opened:
            for path in (ORDERS, ORDERS, INVENTORY, INVENTORY):
                self.api._request('GET', path)
        self.assertEqual(opened.call_count, 4)
        self.assertEqual(self.api._query_cache._pages, {})


class RevenueCacheTests(unittest.TestCase):
    def setUp(self):
        self.api = CoupangAPI('A1', 'not-a-key', 'not-a-secret')
        self.api._request = Mock()
        self.api._query_cache.day = date(2026, 11, 17)

    def page(self, *pairs, token=None, sale_date='2026-11-01'):
        return dict(code='SUCCESS', data=[dict(saleDate=sale_date,
            items=[dict(vendorItemId=oid, productId=pid) for oid, pid in pairs])] if pairs else [],
            hasNext=token is not None, nextToken=token)

    def test_later_target_uses_raw_page_before_original_target_filter(self):
        self.api._request.return_value = self.page(('100', '1000'), ('200', '2000'))
        first = self.api.get_revenue_exposure_map(['100'], lookback_days=31)
        second = self.api.get_revenue_exposure_map(['200'], lookback_days=186)
        self.assertEqual(first['latest']['100']['productId'], '1000')
        self.assertEqual(second['latest']['200']['productId'], '2000')
        self.assertEqual(self.api._request.call_count, 1)
        self.assertEqual(second['requests'], 0)
        self.assertEqual(second['cacheHits'], 1)

    def test_extended_history_fetches_older_chunk_only(self):
        self.api._request.side_effect = [self.page() for _ in range(5)] + [self.page(('200', '2000'), sale_date='2026-10-18')]
        self.api.get_revenue_exposure_map(['200'], lookback_days=31)
        result = self.api.get_revenue_exposure_map(['200'], lookback_days=186)
        self.assertEqual(result['latest']['200']['productId'], '2000')
        self.assertEqual(self.api._request.call_count, 6)
        self.assertEqual(result['cacheHits'], 4)
        first_params = dict(self.api._request.call_args_list[0].args[2])
        second_params = dict(self.api._request.call_args_list[-1].args[2])
        self.assertLess(second_params['recognitionDateTo'], first_params['recognitionDateFrom'])

    def test_failed_incomplete_chunk_is_not_published(self):
        self.api._request.side_effect = [self.page(token='next'), CoupangAPIError('unavailable'),
                                         self.page(('100', '1000'))]
        with self.assertRaises(CoupangAPIError):
            self.api.get_revenue_exposure_map(['100'], lookback_days=31)
        result = self.api.get_revenue_exposure_map(['100'], lookback_days=31)
        self.assertEqual(result['latest']['100']['productId'], '1000')
        self.assertEqual(self.api._request.call_count, 3)
        self.assertEqual(self.api._request.call_args_list[0], self.api._request.call_args_list[2])

    def test_malformed_success_is_not_cached(self):
        self.api._request.side_effect = [dict(code=200, data=None), self.page(('100', '1000'))]
        with self.assertRaises(CoupangAPIError):
            self.api.get_revenue_exposure_map(['100'], lookback_days=31)
        self.api.get_revenue_exposure_map(['100'], lookback_days=31)
        self.assertEqual(self.api._request.call_count, 2)

    def test_repeated_page_token_rejects_without_publishing(self):
        self.api._request.return_value = self.page(token='same')
        with self.assertRaises(CoupangAPIError):
            self.api.get_revenue_exposure_map(['100'], lookback_days=31)
        self.assertEqual(self.api._request.call_count, 2)
        self.assertEqual(self.api._query_cache._pages, {})

    def test_missing_next_token_rejects_without_publishing(self):
        page = self.page()
        page['hasNext'] = True
        self.api._request.return_value = page
        with self.assertRaises(CoupangAPIError):
            self.api.get_revenue_exposure_map(['100'], lookback_days=31)
        self.assertEqual(self.api._query_cache._pages, {})

    def test_cancel_is_checked_even_on_cached_reads(self):
        self.api._request.return_value = self.page(('100', '1000'))
        self.api.get_revenue_exposure_map(['100'], lookback_days=31)
        self.api.cancel_event = threading.Event()
        self.api.cancel_event.set()
        with self.assertRaises(CoupangAPICancelled):
            self.api.get_revenue_exposure_map(['100'], lookback_days=31)
        self.assertEqual(self.api._request.call_count, 1)

    def test_cache_keys_isolate_vendor_parameters_and_api_instance(self):
        self.api._request.return_value = self.page(('100', '1000'))
        self.api.get_revenue_exposure_map(['100'], lookback_days=31)
        self.api.vendor_id = 'A2'
        self.api.get_revenue_exposure_map(['100'], lookback_days=31)
        self.api.get_revenue_exposure_map(['100'], lookback_days=30)
        # Different total lookbacks share the same complete newest 7-day window.
        self.assertEqual(self.api._request.call_count, 2)
        fresh = CoupangAPI('A2', 'not-a-key', 'not-a-secret')
        self.assertEqual(fresh._query_cache._pages, {})

    def test_cached_response_is_not_changed_by_caller_mutation(self):
        raw = self.page(('100', '1000'))
        self.api._request.return_value = raw
        self.api.get_revenue_exposure_map(['100'], lookback_days=31)
        raw['data'][0]['items'][0]['productId'] = '9999'
        result = self.api.get_revenue_exposure_map(['100'], lookback_days=31)
        self.assertEqual(result['latest']['100']['productId'], '1000')

    def test_date_anchor_survives_midnight_and_new_year_until_next_run(self):
        with patch.object(api_query, 'today_kst', side_effect=[date(2026, 12, 31), date(2027, 1, 1)]):
            api = CoupangAPI('A1', 'not-a-key', 'not-a-secret')
            api._request = Mock(return_value=self.page(('100', '1000'), ('200', '2000')))
            api.get_revenue_exposure_map(['100'], lookback_days=31)
            api.get_revenue_exposure_map(['200'], lookback_days=186)
            fresh = CoupangAPI('A1', 'not-a-key', 'not-a-secret')
            fresh._request = Mock(return_value=self.page(('100', '1000')))
            fresh.get_revenue_exposure_map(['100'], lookback_days=31)
        self.assertEqual(api._request.call_count, 1)
        self.assertEqual(dict(api._request.call_args.args[2])['recognitionDateTo'], '2026-12-30')
        self.assertEqual(dict(fresh._request.call_args.args[2])['recognitionDateTo'], '2026-12-31')


if __name__ == '__main__':
    unittest.main()
