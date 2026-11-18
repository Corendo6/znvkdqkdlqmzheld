"""Resolve exposure IDs only from explicit option-level API evidence.

Seller-product-level productId is deliberately not an option mapping. A hybrid
item may contain different marketplace and Rocket Growth identities; only IDs
reported together on the same object are accepted.
"""
LIST_PATH = '/v2/providers/seller_api/apis/api/v1/marketplace/seller-products'
CHANNELS = (
    ('rocketGrowthItemData', 'rocketGrowth'), ('rocketGrowthItem', 'rocketGrowth'),
    ('marketPlaceItemData', 'marketplace'), ('marketplaceItemData', 'marketplace'),
    ('marketPlaceItem', 'marketplace'), ('marketplaceItem', 'marketplace'),
)


def _id(value):
    if isinstance(value, bool):
        return ''
    raw = str(value or '').strip()
    return raw if raw.isascii() and raw.isdigit() and int(raw) > 0 else ''


def extract(products, vendor_id, vendor_item_ids=None, seller_product_id=None,
            source='rg-list', rocket_only=True):
    """Return unambiguous option mappings and inspectable provenance, without writes.

    Exact-pair conflicts are left unresolved, even when one response is newer in
    iteration order. `rocket_only=False` additionally admits marketplace pairs.
    The list endpoint can also return legacy direct item identities; those are
    accepted unless explicitly identified as marketplace-only.
    """
    vendor = str(vendor_id or '').strip()
    if not vendor:
        raise ValueError('노출 ID를 확인할 판매자 ID가 없습니다.')
    targets = None if vendor_item_ids is None else {_id(v) for v in vendor_item_ids} - {''}
    seller = _id(seller_product_id) if seller_product_id is not None else None
    if seller_product_id is not None and not seller:
        raise ValueError('등록상품 ID가 올바르지 않습니다.')
    if not isinstance(products, list):
        raise ValueError('상품 목록 응답 형식이 올바르지 않습니다.')
    evidence, observed = [], set()
    for product in products:
        if not isinstance(product, dict) or str(product.get('vendorId') or '').strip() != vendor:
            raise ValueError('상품 응답의 판매자 ID를 확인할 수 없습니다.')
        sid = _id(product.get('sellerProductId'))
        if not sid or (seller is not None and sid != seller):
            raise ValueError('상품 응답의 등록상품 ID가 조회 대상과 다릅니다.')
        items = product.get('items')
        if items is None:
            items = []
        if not isinstance(items, list):
            raise ValueError('상품 옵션 목록 응답 형식이 올바르지 않습니다.')
        for item in items:
            if not isinstance(item, dict):
                raise ValueError('상품 옵션 응답 형식이 올바르지 않습니다.')
            pairs = []
            for name, channel in CHANNELS:
                value = item.get(name)
                if value is None:
                    continue
                if not isinstance(value, dict):
                    raise ValueError('상품 배송유형 응답 형식이 올바르지 않습니다.')
                if not rocket_only or channel == 'rocketGrowth':
                    pairs.append((value, channel))
            # Direct pair is useful for older item responses. Never associate its
            # productId with a different nested vendorItemId.
            direct_id = _id(item.get('vendorItemId'))
            marketplace_ids = {_id(item.get(name, {}).get('vendorItemId'))
                               for name, channel in CHANNELS if channel == 'marketplace'
                               and isinstance(item.get(name), dict)} - {''}
            rocket_ids = {_id(item.get(name, {}).get('vendorItemId'))
                          for name, channel in CHANNELS if channel == 'rocketGrowth'
                          and isinstance(item.get(name), dict)} - {''}
            if not rocket_only or direct_id not in marketplace_ids or direct_id in rocket_ids:
                pairs.append((item, 'direct-item'))
            for pair, channel in pairs:
                vid, pid = _id(pair.get('vendorItemId')), _id(pair.get('productId'))
                if not vid or (targets is not None and vid not in targets):
                    continue
                observed.add(vid)
                if pid:
                    proof = dict(vendorItemId=vid, productId=pid, sellerProductId=sid,
                                 source=source, channel=channel)
                    if proof not in evidence:
                        evidence.append(proof)
    by_option = {}
    for proof in evidence:
        by_option.setdefault(proof['vendorItemId'], []).append(proof)
    latest, conflicts = {}, {}
    for vid, proofs in by_option.items():
        identities = {(p['productId'], p['sellerProductId']) for p in proofs}
        if len(identities) != 1:
            conflicts[vid] = sorted({pid for pid, sid in identities})
            continue
        pid, sid = identities.pop()
        latest[vid] = dict(productId=pid, sellerProductId=sid, source=source)
    return dict(latest=latest, evidence=evidence, conflicts=conflicts,
                unresolved=sorted((targets if targets is not None else observed) - latest.keys()))


def fetch(api, vendor_item_ids, seller_product_id=None, progress=None):
    """Page the dedicated RG listing via the existing signed/retried client.

    No partial mapping escapes if a later page fails or contradicts an earlier
    page. The endpoint's own response has no guarantee of option productIds;
    absent explicit pairs remain unresolved for revenue-history/manual review.
    """
    targets = {_id(v) for v in vendor_item_ids} - {''}
    if not targets:
        return dict(latest={}, evidence=[], conflicts={}, unresolved=[], requests=0)
    vendor = str(api.vendor_id or '').strip()
    seller = _id(seller_product_id) if seller_product_id is not None else None
    if not vendor or (seller_product_id is not None and not seller):
        raise ValueError('노출 ID 조회 대상 정보가 올바르지 않습니다.')
    params = dict(vendorId=vendor, businessTypes=['rocketGrowth'], maxPerPage=100)
    if seller is not None:
        params['sellerProductId'] = seller
    products, seen_tokens = [], set()
    for page in range(1, 10001):
        api.check_cancelled()
        response = api._request('GET', LIST_PATH, dict(params))
        if not isinstance(response, dict) or str(response.get('code', '')).upper() not in ('SUCCESS', '200'):
            raise ValueError('로켓그로스 상품 목록 조회에 실패했습니다.')
        rows = response.get('data')
        # Validate each page even if no target option was found in that page.
        extract(rows, vendor, targets, seller)
        products.extend(rows)
        if progress:
            progress(f'로켓그로스 옵션별 링크 확인 {page}페이지')
        if 'nextToken' not in response:
            raise ValueError('상품 목록의 다음 페이지 정보가 없습니다.')
        raw_token = response['nextToken']
        token = '' if raw_token is None else str(raw_token).strip()
        if not token:
            if response.get('hasNext') is True:
                raise ValueError('상품 목록의 다음 페이지 토큰이 없습니다.')
            break
        if isinstance(raw_token, (bool, dict, list)) or token in seen_tokens:
            raise ValueError('상품 목록의 다음 페이지 토큰이 잘못되었거나 반복됩니다.')
        seen_tokens.add(token)
        params['nextToken'] = token
        api.wait(0.15)
    else:
        raise ValueError('상품 목록 페이지 수가 너무 많습니다.')
    api.check_cancelled()
    if str(api.vendor_id or '').strip() != vendor:
        raise ValueError('조회 중 판매자 ID가 변경되었습니다.')
    result = extract(products, vendor, targets, seller)
    result['requests'] = page
    return result
