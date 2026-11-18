"""Safe seven-day revenue windows and exact per-option ID evidence."""
from datetime import date, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import api_query
import revenue_exposure as subject


class WindowTests(unittest.TestCase):
    def test_month_year_leap_boundaries_are_disjoint_and_complete(self):
        for first, last, today in [('2026-09-16','2026-10-16','2026-11-17'),
                                   ('2026-12-20','2027-01-02','2027-01-03'),
                                   ('2024-02-20','2024-03-04','2024-03-05')]:
            windows = subject.date_windows(first,last,date.fromisoformat(today))
            days = []
            for start, end in windows:
                self.assertLessEqual((end-start).days,6)
                days += [start+timedelta(days=i) for i in range((end-start).days+1)]
            expected = [date.fromisoformat(first)+timedelta(days=i)
                        for i in range((date.fromisoformat(last)-date.fromisoformat(first)).days+1)]
            self.assertEqual(sorted(days),expected)
            self.assertEqual(len(set(days)),len(days))

    def test_end_today_future_and_reversed_rejected(self):
        for first,last in [('2026-11-01','2026-11-17'),('2026-11-01','2026-11-18'),('2026-11-16','2026-11-15')]:
            with self.assertRaises(ValueError): subject.date_windows(first,last,date(2026,11,17))


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        cache=api_query.QueryCache(); cache.day=date(2026,11,17)
        self.api=SimpleNamespace(vendor_id='VENDOR',REVENUE_PATH='/revenue-history',
            _query_cache=cache,check_cancelled=Mock(),_request=Mock())

    def page(self,items,**extra):
        return dict(code=200,data=[dict(saleDate='2026-11-12',recognitionDate='2026-11-15',items=items)],hasNext=False,**extra)

    def test_exact_option_match_not_neighbor_or_parent(self):
        self.api._request.return_value=self.page([
            dict(vendorItemId='96294832572',productId='12345'),
            dict(vendorItemId='96294832571',productId='99999')],productId='77777')
        result=subject.fetch(self.api,['96294832572'],31)
        self.assertEqual(result['latest'],{'96294832572':dict(productId='12345',seenDate='2026-11-12')})

    def test_latest_day_conflict_has_no_arbitrary_winner(self):
        self.api._request.return_value=self.page([dict(vendorItemId=1,productId=100),dict(vendorItemId=1,productId=200)])
        result=subject.fetch(self.api,['1'],7)
        self.assertEqual(result['latest'],{})
        self.assertEqual(result['unresolved'],['1'])
        self.assertEqual(set(result['history']['1']),{'100','200'})

    def test_zero_and_invalid_id_never_become_links(self):
        self.api._request.return_value=self.page([dict(vendorItemId=1,productId=0),dict(vendorItemId=1,productId='abc'),dict(vendorItemId=True,productId=100)])
        self.assertEqual(subject.fetch(self.api,['1'],7)['latest'],{})

    def test_mismatched_vendor_or_out_of_range_recognition_is_not_cached(self):
        for mutation in ('vendor','date','hasNext'):
            result=self.page([dict(vendorItemId=1,productId=100)])
            if mutation=='vendor': result['data'][0]['vendorId']='OTHER'
            elif mutation=='date': result['data'][0]['recognitionDate']='2026-11-17'
            else: result['hasNext']='false'
            self.api._request.return_value=result
            with self.assertRaises(ValueError): subject.fetch(self.api,['1'],7)
            self.assertEqual(self.api._query_cache._pages,{})

    def test_paginated_window_resets_tokens_and_checks_every_day(self):
        def request(method,path,params,**kwargs):
            params=dict(params)
            return dict(code=200,data=[],hasNext=not bool(params['token']),nextToken='same-per-window' if not params['token'] else '')
        self.api._request.side_effect=request
        result=subject.fetch(self.api,['1'],15)
        calls=[dict(c.args[2]) for c in self.api._request.call_args_list]
        self.assertEqual([c['token'] for c in calls],['','same-per-window']*3)
        self.assertEqual(result['daysScanned'],15)
        self.assertEqual(calls[0]['recognitionDateTo'],'2026-11-16')
        self.assertEqual(calls[-1]['recognitionDateFrom'],'2026-11-02')

    def test_failed_later_page_does_not_publish_partial_window(self):
        first=self.page([dict(vendorItemId=1,productId=100)]); first.update(hasNext=True,nextToken='next')
        self.api._request.side_effect=[first,RuntimeError('network')]
        with self.assertRaises(RuntimeError): subject.fetch(self.api,['1'],7)
        self.assertEqual(self.api._query_cache._pages,{})


if __name__=='__main__': unittest.main()
