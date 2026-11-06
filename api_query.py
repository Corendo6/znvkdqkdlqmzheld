"""Shared request policy; response validation stays with each endpoint's reader."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode


TRANSIENT_CODES = {'429', '500', '502', '503', '504'}


def today_kst():
    return datetime.now(timezone(timedelta(hours=9))).date()


class TransportFailure(Exception):
    """One failed wire attempt, carrying the public error and retry decision."""

    def __init__(self, error, *, retryable=False, network=False):
        super().__init__(str(error))
        self.error = error
        self.retryable = retryable
        self.network = network


def is_rocket_growth(path):
    return '/rg/inventory/' in path or path.endswith('/rg/orders') or '/rg/order/' in path


def request_json(api, method, path, params=None, *, timeout=15, progress=None):
    """One shared budget for transport AND JSON failures, never nested retries.

    The app client supplies a single-attempt transport. Lightweight test/adaptor
    clients may instead supply _request; their transport exceptions propagate.
    Payload/vendor/currency validation is deliberately outside the retry loop.
    """
    rocket = is_rocket_growth(path)
    product_detail = '/marketplace/seller-products/' in path
    attempts = 3 if rocket or product_detail else 2
    once = getattr(api, '_request_once', None)
    for attempt in range(attempts):
        api.check_cancelled()
        try:
            if once is not None:
                response = once(method, path, params, timeout=timeout,
                                attempt=attempt + 1, max_attempts=attempts)
            else:
                response = api._request(method, path, params)
        except TransportFailure as failure:
            if not failure.retryable or attempt == attempts - 1:
                raise failure.error from None
            delay = (1.0 if failure.network else 1.8) * (attempt + 1)
            reason = '네트워크 연결 오류' if failure.network else '쿠팡 일시 오류'
        else:
            if not isinstance(response, dict):
                return response
            code = str(response.get('code', '')).upper()
            if code not in TRANSIENT_CODES or attempt == attempts - 1:
                return response
            delay = 30 * (attempt + 1) if rocket else 1.8 * (attempt + 1)
            message = str(response.get('message') or '').lower()
            reason = ('쿠팡 호출 제한' if code == '429' or '429' in message
                      or 'too many requests' in message else '쿠팡 일시 오류')
        callback = progress or getattr(api, 'progress', None)
        if callback:
            callback(f'{reason} · {delay:g}초 후 재시도 {attempt + 1}/{attempts - 1}')
        api.wait(delay)


class QueryCache:
    """Raw, validated pages scoped to one API client/synchronization run.

    Callers publish a complete page group only after its pagination succeeds.
    Keeping the unfiltered response permits a later resolver to use new targets.
    """

    def __init__(self):
        self._pages = {}
        # One date anchor per run keeps overlapping history chunks identical
        # even when a long sync crosses Korean midnight or a year boundary.
        self.day = today_kst()

    @staticmethod
    def key(vendor, method, path, params=None):
        pairs = list(params.items()) if isinstance(params, dict) else (params or [])
        return str(vendor), method.upper(), path, urlencode(pairs, doseq=True)

    def get(self, key):
        return deepcopy(self._pages.get(key))

    def publish(self, pages):
        self._pages.update(deepcopy(pages))
