"""Read exact option/product pairs from complete seven-day revenue queries.

Revenue recognition is delayed. These are dated observations, not proof of a
current storefront URL; callers must preserve existing verified/manual links.
"""
from datetime import date, timedelta


def date_windows(start, end, today):
    """Newest first, inclusive, disjoint windows of at most seven days."""
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
    except (TypeError, ValueError):
        raise ValueError('매출내역 날짜는 YYYY-MM-DD 형식이어야 합니다.') from None
    if first > last or last >= today:
        raise ValueError('매출내역은 시작일부터 한국시간 어제까지 조회할 수 있습니다.')
    windows = []
    while last >= first:
        begin = max(first, last - timedelta(days=6))
        windows.append((begin, last))
        last = begin - timedelta(days=1)
    return windows


def _positive_id(value):
    if isinstance(value, bool):
        return ''
    text = str(value or '').strip()
    return text if text.isascii() and text.isdigit() and int(text) > 0 else ''


def _day(value):
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError('매출내역에 올바르지 않은 날짜가 있습니다.') from None
    if parsed.isoformat() != value:
        raise ValueError('매출내역 날짜는 YYYY-MM-DD 형식이어야 합니다.')
    return parsed


def fetch(api, vendor_item_ids, lookback_days=365, progress=None):
    targets = {_positive_id(value) for value in vendor_item_ids}
    targets.discard('')
    if isinstance(lookback_days, bool) or not isinstance(lookback_days, int) or lookback_days < 1:
        raise ValueError('매출내역 조회 일수는 1 이상의 정수여야 합니다.')
    vendor = str(api.vendor_id or '').strip()
    if not vendor:
        raise ValueError('매출내역 조회 판매자 ID가 없습니다.')
    today = api._query_cache.day
    end = today - timedelta(days=1)
    first = end - timedelta(days=lookback_days - 1)
    windows = date_windows(first.isoformat(), end.isoformat(), today)
    unresolved, history, ambiguous = set(targets), {}, set()
    requests = cache_hits = scanned_days = 0
    for start, end in windows:
        if not unresolved:
            break
        token, seen_tokens, completed_pages = '', set(), {}
        chunk_history = {}
        for page in range(1, 10001):
            api.check_cancelled()
            if str(api.vendor_id or '').strip() != vendor:
                raise ValueError('매출내역 조회 중 판매자 ID가 변경되었습니다.')
            params = [('vendorId', vendor), ('recognitionDateFrom', start.isoformat()),
                      ('recognitionDateTo', end.isoformat()), ('token', token), ('maxPerPage', 50)]
            key = api._query_cache.key(vendor, 'GET', api.REVENUE_PATH, params)
            result = api._query_cache.get(key)
            if result is None:
                result = api._request('GET', api.REVENUE_PATH, params, timeout=45)
                requests += 1
            else:
                cache_hits += 1
            if not isinstance(result, dict) or str(result.get('code', '')).upper() not in ('200', 'SUCCESS'):
                raise ValueError('매출내역 조회 실패: ' + str(result.get('message', '') if isinstance(result, dict) else '응답 형식 오류'))
            rows = result.get('data')
            if not isinstance(rows, list) or not isinstance(result.get('hasNext'), bool):
                raise ValueError('매출내역 목록 또는 다음 페이지 응답 형식이 올바르지 않습니다.')
            if result.get('vendorId') is not None and str(result['vendorId']) != vendor:
                raise ValueError('매출내역의 판매자 ID가 일치하지 않습니다.')
            for sale in rows:
                if not isinstance(sale, dict) or not isinstance(sale.get('items'), list):
                    raise ValueError('매출내역 상품 목록의 응답 형식이 올바르지 않습니다.')
                if sale.get('vendorId') is not None and str(sale['vendorId']) != vendor:
                    raise ValueError('매출내역의 판매자 ID가 일치하지 않습니다.')
                recognition = _day(sale['recognitionDate']) if sale.get('recognitionDate') else None
                if recognition is not None and not start <= recognition <= end:
                    raise ValueError('매출내역에 조회 구간 밖의 매출인식일이 있습니다.')
                seen = _day(sale.get('saleDate') or sale.get('recognitionDate'))
                if seen > (recognition or end):
                    raise ValueError('매출내역의 결제일이 매출인식일보다 늦습니다.')
                for item in sale['items']:
                    if not isinstance(item, dict):
                        raise ValueError('매출내역 상품 응답 형식이 올바르지 않습니다.')
                    if item.get('vendorId') is not None and str(item['vendorId']) != vendor:
                        raise ValueError('매출내역 상품의 판매자 ID가 일치하지 않습니다.')
                    vid, pid = _positive_id(item.get('vendorItemId')), _positive_id(item.get('productId'))
                    if vid not in unresolved or not pid:
                        continue
                    bucket = chunk_history.setdefault(vid, {})
                    bucket[pid] = max(bucket.get(pid, ''), seen.isoformat())
            completed_pages[key] = result
            if progress:
                progress(f'노출ID 확인 {start}~{end} {page}페이지 / 발견 {len(targets-unresolved)+len(chunk_history)}/{len(targets)}')
            if not result['hasNext']:
                break
            next_token = result.get('nextToken')
            if not isinstance(next_token, str) or not next_token or next_token in seen_tokens:
                raise ValueError('매출내역의 다음 페이지가 누락되거나 반복됩니다.')
            seen_tokens.add(next_token)
            token = next_token
        else:
            raise ValueError('매출내역 페이지 수가 너무 많습니다.')
        api.check_cancelled()
        if str(api.vendor_id or '').strip() != vendor:
            raise ValueError('매출내역 조회 중 판매자 ID가 변경되었습니다.')
        api._query_cache.publish(completed_pages)
        history.update(chunk_history)
        # A mapping is an observation from the newest recognition window that
        # contains this option. Never invent a winner when its newest payment
        # date contains conflicting product IDs. Preserve all evidence for review.
        unresolved -= set(chunk_history)
        scanned_days += (end-start).days + 1
    latest = {}
    for vid, by_pid in history.items():
        newest = max(by_pid.values())
        candidates = [pid for pid, stamp in by_pid.items() if stamp == newest]
        if len(candidates) == 1:
            latest[vid] = {'productId': candidates[0], 'seenDate': newest}
        else:
            ambiguous.add(vid)
    return dict(latest=latest, history=history, daysScanned=scanned_days,
                requests=requests, cacheHits=cache_hits,
                unresolved=sorted(unresolved | ambiguous), ambiguous=sorted(ambiguous))
