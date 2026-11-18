"""Option-level link evidence must never borrow an ID from another option/channel."""
from copy import deepcopy
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

import exposure_lookup


def product(items=None, **changes):
    row = dict(vendorId='A1', sellerProductId=100, productId=999,
               items=items if items is not None else [])
    row.update(changes)
    return row


def channel(vid, pid=None):
    value = dict(vendorItemId=vid)
    if pid is not None:
        value['productId'] = pid
    return value


def response(rows, next_token=''):
    return dict(code='SUCCESS', data=rows, nextToken=next_token)


class ExposureExtractionTests(TestCase):
    def test_seller_level_id_does_not_become_any_option_id(self):
        rows = [product([dict(rocketGrowthItemData=channel(1)),
                         dict(rocketGrowthItemData=channel(2))])]
        result = exposure_lookup.extract(rows, 'A1', ['1', '2'])
        self.assertEqual(result['latest'], {})
        self.assertEqual(result['unresolved'], ['1', '2'])
        rows[0]['items'].pop()
        self.assertEqual(exposure_lookup.extract(rows, 'A1', ['1'])['latest'], {})

    def test_channel_product_id_is_used_only_for_that_exact_option(self):
        rows = [product([dict(rocketGrowthItemData=channel(1),
            marketPlaceItemData=channel(2, 200), productId=300),
            dict(rocketGrowthItemData=channel(3, 400))])]
        result = exposure_lookup.extract(rows, 'A1', ['1', '2', '3'])
        self.assertEqual(set(result['latest']), {'3'})
        self.assertEqual(result['latest']['3']['productId'], '400')
        self.assertEqual(result['evidence'][0]['channel'], 'rocketGrowth')
        both = exposure_lookup.extract(rows, 'A1', ['1', '2', '3'], rocket_only=False)
        self.assertEqual(set(both['latest']), {'2', '3'})

    def test_direct_item_pair_cannot_be_borrowed_by_nested_channel(self):
        rows = [product([dict(vendorItemId=2, productId=200,
            marketPlaceItemData=channel(2), rocketGrowthItemData=channel(1))])]
        self.assertEqual(exposure_lookup.extract(rows, 'A1', ['1', '2'])['latest'], {})
        self.assertEqual(exposure_lookup.extract(rows, 'A1', ['1', '2'],
            rocket_only=False)['latest']['2']['productId'], '200')

    def test_duplicate_conflicting_pairs_are_unresolved_not_last_row_wins(self):
        rows = [product([dict(rocketGrowthItemData=channel(1, 1000))]),
                product([dict(rocketGrowthItemData=channel(1, 2000))])]
        result = exposure_lookup.extract(rows, 'A1', ['1'])
        self.assertEqual(result['latest'], {})
        self.assertEqual(result['conflicts'], {'1': ['1000', '2000']})
        self.assertEqual(result['unresolved'], ['1'])

    def test_same_pair_under_conflicting_seller_product_is_not_accepted(self):
        rows = [product([dict(vendorItemId=1, productId=1000)]),
                product([dict(vendorItemId=1, productId=1000)], sellerProductId=101)]
        self.assertEqual(exposure_lookup.extract(rows, 'A1')['latest'], {})

    def test_duplicate_identical_pair_is_safe_and_ids_must_be_positive_integers(self):
        rows = [product([dict(rocketGrowthItem=channel(1, 1000)),
                         dict(rocketGrowthItem=channel(1, 1000)),
                         dict(rocketGrowthItemData=channel(2, -1)),
                         dict(rocketGrowthItemData=channel(3, '123/evil'))])]
        result = exposure_lookup.extract(rows, 'A1', ['1', '2', '3'])
        self.assertEqual(set(result['latest']), {'1'})
        self.assertEqual(len(result['evidence']), 1)

    def test_wrong_vendor_or_requested_seller_rejects_response(self):
        for row, seller in [(product(vendorId='OTHER'), None),
                            (product(), '101')]:
            with self.subTest(row=row, seller=seller), self.assertRaises(ValueError):
                exposure_lookup.extract([row], 'A1', ['1'], seller)


class ExposurePagingTests(TestCase):
    def api(self, pages):
        return SimpleNamespace(vendor_id='A1', _request=Mock(side_effect=pages),
                               check_cancelled=Mock(), wait=Mock())

    def test_all_pages_are_verified_and_target_filter_sent(self):
        api = self.api([response([product([dict(rocketGrowthItemData=channel(1, 1000))])], 'next'),
                        response([product([dict(rocketGrowthItemData=channel(2, 2000))])])])
        result = exposure_lookup.fetch(api, ['1', '2'], seller_product_id='100')
        self.assertEqual(result['requests'], 2)
        self.assertEqual(set(result['latest']), {'1', '2'})
        params = api._request.call_args_list[0].args[2]
        self.assertEqual(params['businessTypes'], ['rocketGrowth'])
        self.assertEqual(params['vendorId'], 'A1')
        self.assertEqual(params['sellerProductId'], '100')
        self.assertEqual(api._request.call_args_list[1].args[2]['nextToken'], 'next')
        api.wait.assert_called_once_with(.15)

    def test_later_page_conflict_is_not_hidden_by_early_success(self):
        api = self.api([response([product([dict(vendorItemId=1, productId=1000)])], 'next'),
                        response([product([dict(vendorItemId=1, productId=2000)])])])
        result = exposure_lookup.fetch(api, ['1'])
        self.assertEqual(result['latest'], {})
        self.assertEqual(result['unresolved'], ['1'])

    def test_repeated_or_missing_tokens_do_not_return_partial_mapping(self):
        good = response([product([dict(vendorItemId=1, productId=1000)])], 'next')
        missing = deepcopy(good); missing.pop('nextToken')
        for pages in [[good, good], [missing], [dict(code='SUCCESS', data=[], nextToken='', hasNext=True)]]:
            with self.subTest(pages=pages), self.assertRaises(ValueError):
                exposure_lookup.fetch(self.api(pages), ['1'])

    def test_page_failure_and_cancellation_propagate_without_result(self):
        good = response([product([dict(vendorItemId=1, productId=1000)])], 'next')
        with self.assertRaisesRegex(RuntimeError, 'quota'):
            exposure_lookup.fetch(self.api([good, RuntimeError('quota')]), ['1'])
        api = self.api([response([])])
        api.check_cancelled.side_effect = [None, RuntimeError('cancelled')]
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            exposure_lookup.fetch(api, ['1'])

    def test_page_for_another_vendor_or_seller_is_rejected(self):
        with self.assertRaises(ValueError):
            exposure_lookup.fetch(self.api([response([product(vendorId='OTHER')])]), ['1'])
        with self.assertRaises(ValueError):
            exposure_lookup.fetch(self.api([response([product(sellerProductId=101)])]), ['1'], '100')
