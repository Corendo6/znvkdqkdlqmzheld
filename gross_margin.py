# -*- coding: utf-8 -*-
"""Shared, read-only gross-margin calculation using the user's stored inputs."""
import math
import html
import re


def parse_coupang_size(value):
    """Read an explicit size label from copied Coupang text, without inference."""
    text = html.unescape(str(value or '')).replace('\u200b', '')
    text = re.sub(r'[*_`]', '', text).strip()
    match = re.match(r'^(극\s*소형|소형|중형|대형\s*[12]|특\s*대형)(?=$|\s|[-–—:：])', text)
    return normalize_size(re.sub(r'\s+', '', match.group(1))) if match else ''


def number(value):
    try:
        result = float(str(value).replace(',', '').strip())
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def normalize_size(value):
    return {'극소형': '극소형', '소형': '소형', '중형': '중형',
            '대형1': '대형 1', '대형2': '대형 2', '특대형': '특대형'}.get(
                str(value or '').strip().replace(' ', ''), '')


def load_context(db):
    """Five SELECTs, independent of the number of products; never save inferred data."""
    model_map = {str(r['option_id'] or '').strip(): str(r['model'] or '').strip()
                 for r in db.query('SELECT option_id,model FROM option_model_map')}
    profiles = {str(r['model'] or '').strip(): dict(r)
                for r in db.query('SELECT * FROM gross_margin_profiles')}
    refs = {}
    for row in db.query("SELECT * FROM margin_refs WHERE method='로켓그로스' ORDER BY id"):
        refs.setdefault(str(row['model'] or '').strip(), dict(row))
    rates = [dict(r) for r in db.query('SELECT * FROM fulfillment_rates ORDER BY min_price DESC,id')]
    commission = number(db.setting('commission_rate', '7.8'))
    return dict(model_map=model_map, profiles=profiles, refs=refs, rates=rates,
                commission=7.8 if commission is None else commission, sizes={})


def _matches(rate, sale):
    return sale is not None and rate['min_price'] <= sale and (
        rate['max_price'] is None or sale < rate['max_price'])


def _size(model, profile, ref, context):
    if model in context['sizes']:
        return context['sizes'][model]
    size, source = normalize_size(profile.get('size_type')), str(profile.get('source') or '')
    if not size:
        size = normalize_size(ref.get('size_type'))
        source = 'margin-ref' if size else ''
    if not size:
        sale, inbound, shipping = (number(ref.get(k)) for k in ('sale_price', 'inout_fee', 'shipping_fee'))
        if sale and sale > 0 and inbound is not None and shipping is not None:
            candidates = {normalize_size(r['size_type']) for r in context['rates']
                          if _matches(r, sale)
                          and any(v is not None and abs(inbound-float(v)) < .01
                                  for v in (r['inout_current'], r['inout_regular']))
                          and any(v is not None and abs(shipping-float(v)) < .01
                                  for v in (r['shipping_current'], r['shipping_regular']))}
            candidates.discard('')
            if len(candidates) == 1:
                size, source = candidates.pop(), 'legacy-fee-infer'
    context['sizes'][model] = size, source
    return size, source


def evaluate(product, context, allow_reference_price=True):
    """Calculate from the stored product price, without changing product or DB."""
    p = dict(product)
    oid = str(p.get('option_id') or '').strip()
    model = str(p.get('model') or '').strip() or context['model_map'].get(oid, '')
    profile, ref = context['profiles'].get(model, {}), context['refs'].get(model, {})
    size, size_source = _size(model, profile, ref, context)
    sale = number(p.get('sale_price'))
    sale_source = 'API'
    if (sale is None or sale <= 0) and allow_reference_price:
        sale, sale_source = number(ref.get('sale_price')), '기존 엑셀'
    if sale is None or sale <= 0:
        sale, sale_source = None, '미확인'
    cost = number(profile.get('manual_cost'))
    cost_source = str(profile.get('cost_source') or '수동 입력')
    if cost is None or cost <= 0:
        cost, cost_source = number(ref.get('cost')), '기존 마진표'
    if cost is None or cost <= 0:
        cost, cost_source = None, '미입력'
    # Unit cost must be explicitly supplied; do not infer it from model/supply prices.
    rate = next((r for r in context['rates'] if normalize_size(r['size_type']) == size
                 and size and _matches(r, sale)), None)
    def fee(prefix):
        if not rate:
            return None
        value = rate[prefix+'_current']
        value = number(value if value is not None else rate[prefix+'_regular'])
        return value if value is not None and value >= 0 else None
    inbound, shipping = fee('inout'), fee('shipping')
    commission = context['commission']
    settlement = round(sale*(1-commission*1.1/100)) if sale is not None else None
    logistics = (inbound+shipping)*1.1 if inbound is not None and shipping is not None else None
    margin = settlement-cost-logistics if settlement is not None and cost is not None and logistics is not None else None
    margin_rate = margin/sale*100 if margin is not None else None
    estimated_cost = round(sale*commission*1.1/100+logistics) if sale is not None and logistics is not None else None
    missing = []
    if not model: missing.append('모델')
    if sale is None: missing.append('판매가')
    if cost is None: missing.append('원가')
    if not size: missing.append('사이즈')
    if size and sale is not None and not rate: missing.append('요금')
    if rate and inbound is None: missing.append('입출고비')
    if rate and shipping is None: missing.append('배송비')
    return dict(id=p.get('id'), model=model, option_id=oid,
                option_name=str(p.get('option_name') or ''),
                product_name=str(p.get('product_name') or p.get('display_name') or ''),
                registered_product_id=str(p.get('registered_product_id') or ''),
                exposed_product_id=str(p.get('exposed_product_id') or ''),
                sale_status=str(p.get('sale_status') or '미확인'), sale=sale, sale_source=sale_source,
                map_price=number(p.get('map_price')), cost=cost, cost_source=cost_source,
                size=size, size_source=size_source, size_desc=str(ref.get('size_desc') or ''),
                weight=number(ref.get('weight')), inout=inbound, shipping=shipping,
                logistics=logistics, commission=commission, settlement=settlement,
                commission_fee=sale-settlement if sale is not None else None,
                margin=margin, margin_rate=margin_rate, estimated_cost=estimated_cost,
                missing=missing)
