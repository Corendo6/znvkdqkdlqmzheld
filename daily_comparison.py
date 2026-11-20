"""Read-only comparisons of saved Rocket Growth shipped-order API records.

These amounts are provisional, before cancellation/return deductions. An absent
coverage checkpoint is unknown, even when partial order records are present.
"""
from datetime import date, timedelta
from decimal import Decimal

import read_models
import rocket_sales


def _date(value):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except (ValueError, TypeError):
        raise ValueError('비교 날짜를 YYYY-MM-DD 형식으로 입력하세요.') from None


def _percent(current, previous):
    if previous == 0:
        return Decimal(0) if current == 0 else None
    return (Decimal(current) - Decimal(previous)) * 100 / Decimal(previous)


def build_report(conn, vendor, base_date, compare_date, option_ids=None, today=None):
    """Return two-day totals/rankings and five trailing days, without writing.

    Date arguments are canonical ISO strings. ``option_ids=None`` means all
    options; an empty iterable means no options. Missing daily totals are None,
    while a completed day with no orders has genuine zero totals. Product names
    are present-day display metadata; exact option IDs are the only join key.
    """
    base, previous = _date(base_date), _date(compare_date)
    today = today or rocket_sales.today_kst()
    if previous >= base:
        raise ValueError('비교일은 기준일보다 이전 날짜로 지정하세요.')
    if base >= today:
        raise ValueError('일일 비교는 어제까지의 날짜를 지정하세요.')
    try:
        first = base - timedelta(days=4)
    except OverflowError:
        raise ValueError('최근 5일을 조회할 수 있는 기준일을 지정하세요.') from None
    selected = None if option_ids is None else tuple(sorted({str(oid).strip()
        for oid in option_ids if str(oid).strip()}))

    # Collection may commit in another connection. Keep coverage, orders and
    # catalog within one read snapshot. Never commit/rollback a caller's work.
    own_transaction = not conn.in_transaction
    if own_transaction:
        conn.execute('BEGIN')
    try:
        return _build(conn, vendor, base, previous, first, selected)
    finally:
        if own_transaction:
            conn.rollback()


def _build(conn, vendor, base, previous, first, selected):
    start, end = first.isoformat(), base.isoformat()
    coverage = rocket_sales.coverage_status(conn, vendor, start, end)
    stamps = dict(coverage['fetched_at_by_date'])
    legacy = set(coverage['unverified_dates'])
    groups = dict(read_models.daily_api_totals(conn, vendor, start, end, selected))
    if previous < first:
        day = previous.isoformat()
        extra = rocket_sales.coverage_status(conn, vendor, day, day)
        stamps.update(extra['fetched_at_by_date'])
        legacy.update(extra['unverified_dates'])
        groups.update(read_models.daily_api_totals(conn, vendor, day, day, selected))
    catalog = read_models.catalog_snapshot(conn)['products']

    def summarize(day):
        key = day.isoformat()
        complete = key in stamps
        options = {}
        if complete:
            for (paid_day, oid), group in groups.items():
                if paid_day != key:
                    continue
                product = catalog.get(oid, {})
                options[oid] = dict(quantity=group['quantity'], revenue=group['revenue'],
                    product_name=product.get('product_name') or group['product'] or '상품명 확인 필요',
                    option_name=product.get('option_name') or '')
        return dict(date=key, complete=complete,
                    state='complete' if complete else 'unverified' if key in legacy else 'missing',
                    fetched_at=stamps.get(key, ''), options=options,
                    revenue=sum((row['revenue'] for row in options.values()), Decimal(0)) if complete else None,
                    quantity=sum(row['quantity'] for row in options.values()) if complete else None)

    trend = [summarize(first + timedelta(days=i)) for i in range(5)]
    current = trend[-1]
    old = summarize(previous)
    comparable = current['complete'] and old['complete']
    result = dict(base=current, previous=old, trend=trend, comparable=comparable,
        rows=[], increases=[], decreases=[], counts=dict(increased=0, decreased=0, unchanged=0),
        revenue_delta=None, quantity_delta=None, revenue_percent=None, quantity_percent=None)
    if not comparable:
        return result
    for oid in sorted(set(current['options']) | set(old['options'])):
        now = current['options'].get(oid, {})
        before = old['options'].get(oid, {})
        label = now or before
        qty, prev_qty = now.get('quantity', 0), before.get('quantity', 0)
        rev, prev_rev = now.get('revenue', Decimal(0)), before.get('revenue', Decimal(0))
        result['rows'].append(dict(option_id=oid, product_name=label['product_name'],
            option_name=label['option_name'], previous_quantity=prev_qty, quantity=qty,
            quantity_delta=qty-prev_qty, previous_revenue=prev_rev, revenue=rev,
            revenue_delta=rev-prev_rev))
    increasing = sorted((r for r in result['rows'] if r['quantity_delta'] > 0),
                        key=lambda r: (-r['quantity_delta'], r['option_id']))
    decreasing = sorted((r for r in result['rows'] if r['quantity_delta'] < 0),
                        key=lambda r: (r['quantity_delta'], r['option_id']))
    result['increases'], result['decreases'] = increasing[:5], decreasing[:5]
    result['counts'] = dict(increased=len(increasing), decreased=len(decreasing),
                           unchanged=len(result['rows'])-len(increasing)-len(decreasing))
    for metric in ('revenue', 'quantity'):
        result[metric + '_delta'] = current[metric] - old[metric]
        result[metric + '_percent'] = _percent(current[metric], old[metric])
    return result
