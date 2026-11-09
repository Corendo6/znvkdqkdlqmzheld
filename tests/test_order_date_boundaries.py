"""Observed UTC/end-exclusive API dates must cover complete Korean payment days."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rocket_sales
from test_daily_sales import MemoryDB, TODAY, VENDOR


class OrderDateBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.db = MemoryDB()
        self.addCleanup(self.db.conn.close)
        clock = patch.object(rocket_sales, 'today_kst', return_value=TODAY)
        clock.start()
        self.addCleanup(clock.stop)
        self.last = TODAY - timedelta(days=1)
        self.first = TODAY - timedelta(days=30)

    def raw(self, day, hour=12, minute=0, oid='1001', qty=1):
        paid = datetime.combine(day, datetime.min.time(), tzinfo=rocket_sales.KST)
        paid += timedelta(hours=hour, minutes=minute)
        return dict(vendorId=VENDOR, orderId=oid, paidAt=int(paid.timestamp()*1000),
                    orderItems=[dict(vendorItemId='101', currency='KRW', salesQuantity=qty,
                                     unitSalesPrice='1000', productName='Product')])

    def endpoint(self, rows, paginate=False):
        requests = []
        def request(method, path, params=None):
            requests.append(dict(params))
            first = datetime.strptime(params['paidDateFrom'], '%Y%m%d').replace(tzinfo=timezone.utc)
            last = datetime.strptime(params['paidDateTo'], '%Y%m%d').replace(tzinfo=timezone.utc)
            data = [row for row in rows if first.timestamp()*1000 <= row['paidAt'] < last.timestamp()*1000]
            if paginate:
                middle = len(data)//2
                return dict(code=200, data=data[middle:] if params.get('nextToken') else data[:middle],
                            nextToken=None if params.get('nextToken') else 'same-token-each-window')
            return dict(code=200, data=data)
        return SimpleNamespace(vendor_id=VENDOR, _request=request,
                               check_cancelled=Mock(), requests=requests)

    def test_single_day_includes_korean_midnight_morning_and_late_night(self):
        rows = [self.raw(self.last, hour, minute, str(index+1000))
                for index, (hour, minute) in enumerate(((0,0),(8,59),(9,0),(23,59)))]
        rows += [self.raw(self.last-timedelta(days=1),23,59,'2001'), self.raw(TODAY,0,0,'2002')]
        api = self.endpoint(rows)
        orders = rocket_sales.fetch_orders(api, self.last.isoformat(), self.last.isoformat())
        self.assertEqual({o['order_id'] for o in orders}, {'1000','1001','1002','1003'})
        self.assertEqual(api.requests, [dict(paidDateFrom=(self.last-timedelta(days=1)).strftime('%Y%m%d'),
                                            paidDateTo=TODAY.strftime('%Y%m%d'))])

    def test_thirty_korean_days_are_complete_and_tokens_reset_for_each_chunk(self):
        rows = [self.raw(self.first+timedelta(days=day),hour,minute,str(10000+day*4+index))
                for day in range(30)
                for index,(hour,minute) in enumerate(((0,0),(8,59),(9,0),(23,59)))]
        api = self.endpoint(rows, paginate=True)
        orders = rocket_sales.fetch_orders(api, self.first.isoformat(), self.last.isoformat())
        self.assertEqual(len(orders), 120)
        self.assertEqual(len({o['order_id'] for o in orders}),120)
        self.assertEqual(len(api.requests),4)
        self.assertNotIn('nextToken',api.requests[0])
        self.assertNotIn('nextToken',api.requests[2])
        self.assertEqual(api.requests[1]['nextToken'],api.requests[3]['nextToken'])
        for args in api.requests:
            span = datetime.strptime(args['paidDateTo'],'%Y%m%d')-datetime.strptime(args['paidDateFrom'],'%Y%m%d')
            self.assertLessEqual(span.days,29)
        rocket_sales.save_orders(self.db.conn,VENDOR,orders,self.first.isoformat(),self.last.isoformat())
        quantities = rocket_sales.daily_quantities(self.db.conn,VENDOR,TODAY)
        self.assertEqual(quantities['by_option']['101'],dict(yesterday=4,last7=28,sales_30=120))
        self.assertEqual(quantities['coverage'],{1:1,7:7,30:30})

    def test_failure_in_later_chunk_leaves_existing_orders_and_coverage_untouched(self):
        before = list(self.db.conn.iterdump())
        requests = Mock(side_effect=[dict(code=200,data=[self.raw(self.first)]), dict(code=400,message='bad range')])
        api = SimpleNamespace(vendor_id=VENDOR,_request=requests,check_cancelled=Mock())
        with self.assertRaises(ValueError):
            orders = rocket_sales.fetch_orders(api,self.first.isoformat(),self.last.isoformat())
            rocket_sales.save_orders(self.db.conn,VENDOR,orders,self.first.isoformat(),self.last.isoformat())
        self.assertEqual(list(self.db.conn.iterdump()),before)
        self.assertEqual(requests.call_count,2)

    def test_cancel_between_chunks_prevents_second_query(self):
        requests = Mock(return_value=dict(code=200,data=[self.raw(self.first)]))
        api = SimpleNamespace(vendor_id=VENDOR,_request=requests,
                              check_cancelled=Mock(side_effect=[None,None,RuntimeError('cancel')]))
        # First loop and shared executor check cancellation; second loop aborts.
        with self.assertRaisesRegex(RuntimeError,'cancel'):
            rocket_sales.fetch_orders(api,self.first.isoformat(),self.last.isoformat())
        self.assertEqual(requests.call_count,1)

    def test_invalid_vendor_in_padding_is_rejected_not_silently_filtered(self):
        row = self.raw(self.last-timedelta(days=1),23,59)
        row['vendorId']='OTHER'
        api = self.endpoint([row])
        with self.assertRaisesRegex(ValueError,'판매자'):
            rocket_sales.fetch_orders(api,self.last.isoformat(),self.last.isoformat())

    def test_repeated_order_across_windows_uses_one_canonical_detail(self):
        fragment=self.raw(self.last,qty=1)
        complete=self.raw(self.last,qty=3)
        request=Mock(side_effect=[dict(code=200,data=[fragment]),dict(code=200,data=[fragment]),
                                 dict(code=200,data=complete)])
        api=SimpleNamespace(vendor_id=VENDOR,_request=request,check_cancelled=Mock())
        orders=rocket_sales.fetch_orders(api,self.first.isoformat(),self.last.isoformat())
        self.assertEqual(len(orders),1)
        self.assertEqual(orders[0]['items']['101']['quantity'],3)
        self.assertEqual(sum('/rg/order/' in c.args[1] for c in request.call_args_list),1)


if __name__=='__main__':
    unittest.main()
