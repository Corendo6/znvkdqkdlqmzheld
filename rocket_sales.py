"""Rocket Growth shipped orders, recorded by their Korean payment date."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import api_query
import read_models

KST = timezone(timedelta(hours=9))
SOURCE_PREFIX = 'rg-order-api:'
ORDER_PATH = '/v2/providers/rg_open_api/apis/api/v1/vendors/{vendorId}/rg/orders'
ORDER_DETAIL_PATH = '/v2/providers/rg_open_api/apis/api/v1/vendors/{vendorId}/rg/order/{orderId}'


def today_kst():
    return datetime.now(KST).date()


def validate_period(start, end):
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
    except (ValueError, TypeError):
        raise ValueError('매출 조회 날짜를 YYYY-MM-DD 형식으로 입력하세요.')
    if last < first or (last - first).days >= 30:
        raise ValueError('매출 조회 기간은 시작일을 포함하여 1~30일로 지정하세요.')
    if last >= today_kst():
        raise ValueError('일일 매출은 전일까지 조회합니다. 종료일을 전일 이전으로 지정하세요.')
    return first, last


def ensure_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS rg_order_items(
        vendor_id TEXT NOT NULL, order_id TEXT NOT NULL, option_id TEXT NOT NULL,
        paid_date TEXT NOT NULL, product_name TEXT NOT NULL DEFAULT '',
        quantity INTEGER NOT NULL, revenue TEXT NOT NULL, fetched_at TEXT NOT NULL,
        PRIMARY KEY(vendor_id,order_id,option_id))''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_rg_orders_date ON rg_order_items(vendor_id,paid_date,option_id)')
    had_coverage = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='rg_sales_coverage'").fetchone()
    conn.execute('''CREATE TABLE IF NOT EXISTS rg_sales_coverage(
        vendor_id TEXT NOT NULL, sale_date TEXT NOT NULL, fetched_at TEXT NOT NULL,
        PRIMARY KEY(vendor_id,sale_date))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS rg_sales_legacy_coverage(
        vendor_id TEXT NOT NULL, sale_date TEXT NOT NULL, fetched_at TEXT NOT NULL,
        PRIMARY KEY(vendor_id,sale_date))''')
    coverage_version = conn.execute("SELECT value FROM settings WHERE key='rg_orders_coverage_version'").fetchone()
    if not coverage_version:
        # Pre-v0.4.21 date bounds omitted part of each Korean payment day.
        # Old checkpoints have no per-date provenance, so preserve the ledger
        # while requiring one verified fetch before calling those dates complete.
        if had_coverage:
            conn.execute('INSERT OR REPLACE INTO rg_sales_legacy_coverage SELECT * FROM rg_sales_coverage')
            conn.execute('DELETE FROM rg_sales_coverage')
        conn.execute("INSERT INTO settings(key,value) VALUES ('rg_orders_coverage_version','1')")
    if not had_coverage:
        # Older versions retained only the most recent successful query range.
        # Do not infer coverage from dates containing orders: empty days matter.
        settings = dict(conn.execute('SELECT key,value FROM settings'))
        prefix = 'rg_orders_last_sync:'
        for key, stamp in settings.items():
            if not key.startswith(prefix) or not stamp:
                continue
            vendor = key[len(prefix):]
            try:
                datetime.fromisoformat(stamp)
                first = date.fromisoformat(settings.get('rg_orders_last_from:' + vendor, ''))
                last = date.fromisoformat(settings.get('rg_orders_last_to:' + vendor, ''))
            except (ValueError, TypeError):
                continue
            if vendor and 0 <= (last - first).days < 30:
                target = 'rg_sales_coverage' if coverage_version else 'rg_sales_legacy_coverage'
                conn.executemany(f'INSERT OR REPLACE INTO {target} VALUES (?,?,?)',
                                 [(vendor, (first + timedelta(days=i)).isoformat(), stamp)
                                  for i in range((last - first).days + 1)])


def _save_coverage(conn, vendor, first, last, stamp):
    conn.executemany('''INSERT INTO rg_sales_coverage(vendor_id,sale_date,fetched_at)
        VALUES (?,?,?) ON CONFLICT(vendor_id,sale_date) DO UPDATE SET fetched_at=excluded.fetched_at''',
        [(vendor, (first + timedelta(days=i)).isoformat(), stamp)
         for i in range((last - first).days + 1)])


def _fetched_time(value):
    try:
        stamp = datetime.fromisoformat(value)
        return (stamp.replace(tzinfo=KST) if stamp.tzinfo is None else stamp).astimezone(KST)
    except (TypeError, ValueError):
        return None


def coverage_status(conn, vendor, start, end):
    """Read explicit successful date checkpoints, including complete zero-order days."""
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if not 0 <= (last - first).days < 30:
        raise ValueError('수집 상태 조회 기간은 1~30일로 지정하세요.')
    days = [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]
    stamps = dict(conn.execute('''SELECT sale_date,fetched_at FROM rg_sales_coverage
        WHERE vendor_id=? AND sale_date BETWEEN ? AND ?''', (vendor, start, end)))
    completed = [day for day in days if day in stamps]
    missing = [day for day in days if day not in stamps]
    legacy = {row[0] for row in conn.execute('''SELECT sale_date FROM rg_sales_legacy_coverage
        WHERE vendor_id=? AND sale_date BETWEEN ? AND ?''', (vendor, start, end))}
    times = [stamp for value in stamps.values() if (stamp := _fetched_time(value)) is not None]
    return dict(completed_dates=completed, missing_dates=missing, fetched_at_by_date=stamps,
                unverified_dates=[day for day in missing if day in legacy],
                never_collected_dates=[day for day in missing if day not in legacy],
                latest_fetched_at=max(times).isoformat(timespec='seconds') if times else '',
                oldest_fetched_at=min(times).isoformat(timespec='seconds') if times else '',
                total=len(days), count=len(completed), complete=not missing)


def coverage_label(status):
    label = f"수집 {status['count']}/{status['total']}일"
    if status['missing_dates']:
        label += f" · 미수집 {len(status['never_collected_dates'])}일"
    if status['unverified_dates']:
        label += f" · 이전 기록 재확인 {len(status['unverified_dates'])}일"
    stamp = status['latest_fetched_at']
    return label + (' · 최근 저장 ' + stamp.replace('T', ' ')[:19] + ' (한국시간)' if stamp else
                    ' · 새 기준 수집 전' if status['unverified_dates'] else ' · 저장 기록 없음')


def daily_quantities(conn, vendor, today=None):
    """API-only quantities by option, through yesterday KST; incomplete windows are unknown."""
    today = today or today_kst()
    last = (today - timedelta(days=1)).isoformat()
    starts = {n: (today - timedelta(days=n)).isoformat() for n in (1, 7, 30)}
    status = {n: coverage_status(conn, vendor, start, last) for n, start in starts.items()}
    coverage = {n: state['count'] for n, state in status.items()}
    metrics = {'yesterday': 1, 'last7': 7, 'sales_30': 30}
    empty = {key: 0 if coverage[n] == n else None for key, n in metrics.items()}
    by_option = {}
    for (day, oid), group in read_models.daily_api_totals(conn, vendor, starts[30], last).items():
        totals = by_option.setdefault(oid, dict(empty))
        for key, n in metrics.items():
            if totals[key] is not None and day >= starts[n]:
                totals[key] += group['quantity']
    return dict(by_option=by_option, coverage=coverage, empty=empty, status=status)


def _id(value, name):
    raw = str(value or '').strip()
    if not raw.isdigit() or int(raw) <= 0:
        raise ValueError(f'{name}가 없거나 올바르지 않은 주문 응답입니다.')
    return raw


def _number(value, name, integer=False):
    if value is None or isinstance(value, bool):
        raise ValueError(f'{name}가 없는 주문 응답입니다.')
    try:
        number = Decimal(str(value))
    except (ValueError, InvalidOperation):
        raise ValueError(f'{name}가 숫자가 아닌 주문 응답입니다.')
    if not number.is_finite() or number < 0 or (integer and number != number.to_integral_value()):
        raise ValueError(f'{name}가 올바르지 않은 주문 응답입니다.')
    return int(number) if integer else number


def _paid_date(value):
    # List-query paidAt is an epoch-millisecond value, unlike the single-order API.
    stamp = _number(value, '결제일시', integer=True)
    try:
        return datetime.fromtimestamp(stamp / 1000, KST).date().isoformat()
    except (ValueError, OverflowError, OSError):
        raise ValueError('결제일시를 읽을 수 없는 주문 응답입니다.')


def normalize_order(order, vendor, first, last):
    if not isinstance(order, dict) or str(order.get('vendorId') or '') != vendor:
        raise ValueError('판매자 ID가 일치하지 않는 주문 응답입니다.')
    order_id = _id(order.get('orderId'), '주문번호')
    paid = _paid_date(order.get('paidAt'))
    if not first <= paid <= last:
        raise ValueError('조회 기간 밖의 결제일이 반환되었습니다. 이전 매출을 유지합니다.')
    items = order.get('orderItems')
    if not isinstance(items, list):
        raise ValueError('주문 상품 목록이 없는 응답입니다.')
    grouped = {}
    for item in items:
        if not isinstance(item, dict) or item.get('currency') != 'KRW':
            raise ValueError('원화(KRW) 주문 응답인지 확인할 수 없습니다.')
        oid = _id(item.get('vendorItemId'), '옵션 ID')
        quantity = _number(item.get('salesQuantity'), '판매수량', integer=True)
        amount = _number(item.get('unitSalesPrice'), '판매 단가') * quantity
        row = grouped.setdefault(oid, dict(option_id=oid, quantity=0, revenue=Decimal(0),
                                           product_name=str(item.get('productName') or '').strip()))
        row['quantity'] += quantity
        row['revenue'] += amount
    return dict(order_id=order_id, paid_date=paid, items=grouped)


def _request_sales(api, path, params=None, progress=None):
    """Use the same bounded request policy as inventory and catalog queries."""
    return api_query.request_json(api, 'GET', path, params, progress=progress)


def fetch_order_detail(api, order_id, first, last, progress=None):
    """Resolve repeated list entries against the complete order, not guessed sums."""
    api.check_cancelled()
    response = _request_sales(api, ORDER_DETAIL_PATH.format(vendorId=api.vendor_id, orderId=order_id), progress=progress)
    if not isinstance(response, dict) or str(response.get('code')).upper() not in ('200', 'SUCCESS'):
        reason = f"{response.get('code', '')} {response.get('message', '')}".strip() if isinstance(response, dict) else '응답 형식 오류'
        raise ValueError('중복 주문의 상세 조회에 실패했습니다: ' + reason + '\n이전 매출을 유지합니다.')
    raw = response.get('data')
    if isinstance(raw, list) and len(raw) == 1:
        raw = raw[0]
    if not isinstance(raw, dict) or str(raw.get('orderId')) != order_id:
        raise ValueError('주문 상세 응답의 주문번호를 확인할 수 없습니다. 이전 매출을 유지합니다.')
    raw = dict(raw)
    paid = raw.get('paidAt')
    # Live API uses milliseconds/unitSalesPrice; the documented detail format
    # also allows an ISO payment datetime and salesPrice.
    if isinstance(paid, str) and 'T' in paid:
        try:
            stamp = datetime.fromisoformat(paid.replace('Z', '+00:00'))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=KST)
            raw['paidAt'] = int(stamp.timestamp() * 1000)
        except (ValueError, OverflowError, OSError):
            raise ValueError('주문 상세의 결제일시를 읽을 수 없습니다.') from None
    if isinstance(raw.get('orderItems'), list):
        raw['orderItems'] = [dict(item, unitSalesPrice=item.get('unitSalesPrice', item.get('salesPrice')))
                             if isinstance(item, dict) else item for item in raw['orderItems']]
    order = normalize_order(raw, api.vendor_id, first, last)
    api.check_cancelled()
    return order


def _order_query_windows(first, last):
    """Cover KST days using the API's observed UTC, end-exclusive date bounds.

    Live verification on 2026-11-09: 20261108..20261108 returned no orders;
    20261108..20261109 returned paidAt from 09:00 KST through next-day 08:59.
    Pad both edges, split into at most 30 date labels, then filter by paidAt KST.
    """
    cursor = first - timedelta(days=1)
    stop = last + timedelta(days=1)
    while cursor < stop:
        boundary = min(cursor + timedelta(days=29), stop)
        yield cursor, boundary
        cursor = boundary


def fetch_orders(api, start, end, progress=None):
    first, last = validate_period(start, end)
    padded_first = (first - timedelta(days=1)).isoformat()
    padded_last = (last + timedelta(days=1)).isoformat()
    path = ORDER_PATH.format(vendorId=api.vendor_id)
    orders, repeated_orders, selected_orders = {}, set(), set()
    for window, (query_first, query_last) in enumerate(_order_query_windows(first, last), 1):
        params = {'paidDateFrom': query_first.strftime('%Y%m%d'),
                  'paidDateTo': query_last.strftime('%Y%m%d')}
        seen_tokens = set()
        for page in range(1, 10001):
            api.check_cancelled()
            api.phase = f'로켓그로스 일일 매출 {window}구간 {page}페이지'
            response = _request_sales(api, path, params, progress)
            if not isinstance(response, dict) or str(response.get('code')).upper() not in ('200', 'SUCCESS'):
                raise ValueError('주문 조회 실패: ' + str(response.get('message', '') if isinstance(response, dict) else '응답 형식 오류'))
            if not isinstance(response.get('data'), list):
                raise ValueError('주문 목록이 없는 응답입니다. 이전 매출을 유지합니다.')
            for raw in response['data']:
                order = normalize_order(raw, api.vendor_id, padded_first, padded_last)
                if start <= order['paid_date'] <= end:
                    selected_orders.add(order['order_id'])
                old = orders.get(order['order_id'])
                if old is None:
                    orders[order['order_id']] = order
                else:
                    # Padding/chunk boundaries never justify guessed sums: overlapping
                    # options and inconsistent payment dates still require the detail API.
                    if old['paid_date'] != order['paid_date'] or set(old['items']) & set(order['items']):
                        repeated_orders.add(order['order_id'])
                    old['items'].update(order['items'])
            if progress:
                progress(f'로켓그로스 일일 매출 {window}구간 {page}페이지 · {len(selected_orders):,}개 주문 수신')
            token = response.get('nextToken')
            if token in (None, ''):
                break
            token = str(token)
            if token in seen_tokens:
                raise ValueError('주문 조회 페이지가 반복되었습니다. 이전 매출을 유지합니다.')
            seen_tokens.add(token)
            params['nextToken'] = token
        else:
            raise ValueError('주문 조회 페이지 수가 너무 많습니다. 기간을 줄여 다시 조회하세요.')
    api.check_cancelled()
    for index, oid in enumerate(sorted(repeated_orders), 1):
        api.phase = f'나뉜 주문 상세 확인 {index}/{len(repeated_orders)}'
        if progress:
            progress(api.phase)
        # An order observed inside the requested range must not silently move out
        # of that range during canonical resolution. Padding-only orders may.
        detail_first, detail_last = (start, end) if oid in selected_orders else (padded_first, padded_last)
        complete = fetch_order_detail(api, oid, detail_first, detail_last, progress)
        if not set(orders[oid]['items']).issubset(complete['items']):
            raise ValueError('주문 상세에서 목록의 일부 옵션을 확인하지 못했습니다. 다시 조회하세요.')
        orders[oid] = complete
    api.check_cancelled()
    return [order for order in orders.values() if start <= order['paid_date'] <= end]


def prefer_external_sale(conn, paid_date, model, method, option_id=''):
    """A user/Excel total takes precedence over API-derived rows, never over raw orders."""
    if method != '로켓그로스':
        return
    if option_id:
        where, args = 'option_id=?', [str(option_id)]
    else:
        if not str(model or '').strip():
            return
        where, args = 'model=?', [model]
    conn.execute(f"DELETE FROM sales WHERE sale_date=? AND method='로켓그로스' AND source LIKE ? AND {where}",
                 [paid_date, SOURCE_PREFIX + '%', *args])


def _sale_inputs(conn, vendor, start=None, end=None, option_ids=None):
    """Common projection and precedence rules for rebuilding and status reads."""
    grouped = read_models.daily_api_totals(conn, vendor, start, end, option_ids)
    catalog = read_models.catalog_snapshot(conn)
    mappings = {oid: model for oid, model in catalog['model_map'].items() if model}
    external_options, external_models = set(), set()
    source = SOURCE_PREFIX + vendor
    for row in conn.execute("SELECT sale_date,option_id,model FROM sales WHERE method='로켓그로스' AND COALESCE(source,'')<>?", (source,)):
        oid = str(row['option_id'] or '')
        external_options.add((row['sale_date'], oid))
        if not oid:
            external_models.add((row['sale_date'], str(row['model'] or '')))
    pending = {(row['sale_date'], row['option_id']) for row in conn.execute(
        "SELECT sale_date,option_id FROM sales_pending WHERE method='로켓그로스'")}
    return grouped, catalog['products'], mappings, external_options | pending, external_models


def _sale_identity(paid, oid, products, mappings, conflicts, external_models):
    product = products.get(oid, {})
    model = str(mappings.get(oid) or product.get('model') or '').strip()
    conflict = (paid, oid) in conflicts or bool(model and (paid, model) in external_models)
    return product, model, conflict


def sales_status(conn, vendor, start=None, end=None, option_ids=None):
    """Count current conflicts/unmapped API totals without writing sales or mappings."""
    grouped, products, mappings, conflicts, external_models = _sale_inputs(
        conn, vendor, start, end, option_ids)
    result = dict(conflicts=0, unmapped=0)
    for paid, oid in grouped:
        _, model, conflict = _sale_identity(paid, oid, products, mappings, conflicts, external_models)
        if conflict:
            result['conflicts'] += 1
        elif not model:
            result['unmapped'] += 1
    return result


def rebuild_sales(conn, vendor, start=None, end=None, option_ids=None):
    """Rebuild owned daily totals only. Caller owns the transaction."""
    source = SOURCE_PREFIX + vendor
    sale_where, sale_params = 'source=?', [source]
    if start is not None and end is not None:
        sale_where += ' AND sale_date BETWEEN ? AND ?'
        sale_params += [start, end]
    if option_ids is not None:
        if not option_ids:
            return dict(inserted=0, updated=0, unchanged=0, conflicts=0, unmapped=0)
        marks = ','.join('?' for _ in option_ids)
        sale_where += f' AND option_id IN ({marks})'
        sale_params += list(option_ids)
    grouped, products, mappings, conflicts, external_models = _sale_inputs(
        conn, vendor, start, end, option_ids)
    current = conn.execute('SELECT * FROM sales WHERE ' + sale_where, sale_params).fetchall()
    owned = {}
    for row in current:
        owned.setdefault((row['sale_date'], row['option_id']), []).append(row)
    result = dict(inserted=0, updated=0, unchanged=0, conflicts=0, unmapped=0)
    unmatched, matched = {}, set()
    for key in sorted(set(grouped) | set(owned)):
        paid, oid = key
        rows = owned.get(key, [])
        group = grouped.get(key)
        product, model, conflict = _sale_identity(paid, oid, products, mappings, conflicts, external_models)
        if group is None or conflict:
            if rows:
                conn.executemany('DELETE FROM sales WHERE id=?', [(r['id'],) for r in rows])
            if group is not None:
                result['conflicts'] += 1
            continue
        name = str(product.get('option_name') or group['product'] or '')
        title = str(product.get('product_name') or group['product'] or '')
        registered = str(product.get('registered_product_id') or '')
        values = (model, '로켓그로스', float(group['revenue']), group['orders'], group['quantity'],
                  oid, name, title, registered, source)
        if rows:
            fields = ('model','method','revenue','orders','quantity','option_id','option_name','product_name','registered_product_id','source')
            previous = tuple(rows[0][field] if rows[0][field] is not None else '' for field in fields)
            if previous != values:
                conn.execute('''UPDATE sales SET model=?,method=?,revenue=?,orders=?,quantity=?,option_id=?,
                    option_name=?,product_name=?,registered_product_id=?,source=? WHERE id=?''', values + (rows[0]['id'],))
                result['updated'] += 1
            else:
                result['unchanged'] += 1
            if len(rows) > 1:
                conn.executemany('DELETE FROM sales WHERE id=?', [(r['id'],) for r in rows[1:]])
        else:
            conn.execute('''INSERT INTO sales(sale_date,model,method,revenue,orders,quantity,option_id,
                option_name,product_name,registered_product_id,source) VALUES (?,?,?,?,?,?,?,?,?,?,?)''', (paid,) + values)
            result['inserted'] += 1
        if not model:
            result['unmapped'] += 1
            unmatched[oid] = (oid, name, title, registered)
        else:
            matched.add(oid)
    # Each option needs one mapping update, regardless of how many days it sold.
    conn.executemany('''INSERT INTO unmatched_options(option_id,option_name,product_name,registered_product_id,method)
        VALUES (?,?,?,?,'로켓그로스') ON CONFLICT(option_id) DO UPDATE SET
        option_name=excluded.option_name,product_name=excluded.product_name,last_seen=CURRENT_TIMESTAMP''',
                     unmatched.values())
    conn.executemany('DELETE FROM unmatched_options WHERE option_id=?', [(oid,) for oid in matched])
    return result


def save_orders(conn, vendor, orders, start, end, *, clear_error=True, remaining_dates=None):
    """Commit only after every page succeeds. Repeated queries replace the same orders."""
    first, last = validate_period(start, end)
    stamp = datetime.now(KST).isoformat(timespec='seconds')
    affected = [start, end]
    with conn:
        for order in orders:
            old = conn.execute('SELECT paid_date FROM rg_order_items WHERE vendor_id=? AND order_id=? LIMIT 1',
                               (vendor, order['order_id'])).fetchone()
            if old:
                affected.append(old['paid_date'])
            conn.execute('DELETE FROM rg_order_items WHERE vendor_id=? AND order_id=?', (vendor, order['order_id']))
            for oid, item in order['items'].items():
                conn.execute('''INSERT INTO rg_order_items(vendor_id,order_id,option_id,paid_date,product_name,
                    quantity,revenue,fetched_at) VALUES (?,?,?,?,?,?,?,?)''',
                             (vendor, order['order_id'], oid, order['paid_date'], item['product_name'],
                              item['quantity'], str(item['revenue']), stamp))
        # Missing orders are not interpreted as cancellations: that status is not provided.
        result = rebuild_sales(conn, vendor, min(affected), max(affected))
        _save_coverage(conn, vendor, first, last, stamp)
        result['orders'] = len(orders)
        settings = [('last_sync', stamp), ('last_from', start), ('last_to', end)]
        if clear_error:
            settings.append(('last_error', ''))
        if remaining_dates is not None:
            settings.append(('remaining_dates', ','.join(remaining_dates)))
        for key, value in settings:
            conn.execute('INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)', ('rg_orders_' + key + ':' + vendor, value))
        conn.execute('INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)',
                     ('rg_orders_last_result:' + vendor, summary(result)))
    return result


class CollectionError(RuntimeError):
    """A failed date, with already committed dates and the original error intact."""

    def __init__(self, failed_date, result, cause):
        self.failed_date, self.result, self.cause = failed_date, result, cause
        super().__init__(f"{failed_date} 수집 중단 · 이번 실행 {len(result['completed_dates'])}일 저장"
                         f" · 남은 조회 {len(result['remaining_dates'])}일 · {cause}")


def collect_orders(conn, api, start, end, progress=None, force=False, on_saved=None):
    """Checkpoint complete KST days; resume gaps before refreshing recent shipped orders.

    A date is saved only after all pages and canonical order details succeed.
    Recent seven days remain provisional and refresh once per Korean calendar
    day. An explicit force request refreshes even already-covered dates.
    This function owns commits, so callers must finish other transactions first.
    """
    first, last = validate_period(start, end)
    if conn.in_transaction:
        raise ValueError('일일 매출 수집 전에 진행 중인 저장 작업을 완료하세요.')
    vendor = str(api.vendor_id or '').strip()
    if not vendor:
        raise ValueError('일일 매출을 조회할 판매자 ID가 없습니다.')
    initial = coverage_status(conn, vendor, start, end)
    pending_row = conn.execute('SELECT value FROM settings WHERE key=?',
                               ('rg_orders_remaining_dates:' + vendor,)).fetchone()
    pending = set()
    for value in (pending_row[0] if pending_row else '').split(','):
        try:
            pending.add(date.fromisoformat(value).isoformat())
        except ValueError:
            pass
    outside_pending = sorted(day for day in pending if not start <= day <= end)
    today = today_kst()
    recent_start = (today - timedelta(days=7)).isoformat()
    missing = sorted(initial['missing_dates'], reverse=True)
    refresh = []
    for day in reversed(initial['completed_dates']):
        fetched = _fetched_time(initial['fetched_at_by_date'][day])
        if force or day in pending or (day >= recent_start and (fetched is None or fetched.date() < today)):
            refresh.append(day)
    planned = missing + refresh
    result = dict(orders=0, inserted=0, updated=0, unchanged=0, conflicts=0, unmapped=0,
                  completed_dates=[], skipped_dates=[day for day in initial['completed_dates'] if day not in refresh],
                  remaining_dates=list(planned), coverage=initial)
    with conn:
        conn.executemany('INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)', [
            ('rg_orders_last_request_from:' + vendor, start),
            ('rg_orders_last_request_to:' + vendor, end),
            ('rg_orders_remaining_dates:' + vendor, ','.join(outside_pending + planned))])
    for index, day in enumerate(planned):
        try:
            api.check_cancelled()
            if str(api.vendor_id or '').strip() != vendor:
                raise ValueError('수집 중 판매자 ID가 변경되었습니다. 다시 조회하세요.')
            if progress:
                progress(f'일일 매출 {day} · {index + 1}/{len(planned)}일 조회'
                         f" · {len(result['completed_dates'])}일 저장 완료")
            orders = fetch_orders(api, day, day, progress)
            api.check_cancelled()
            if str(api.vendor_id or '').strip() != vendor:
                raise ValueError('수집 중 판매자 ID가 변경되었습니다. 다시 조회하세요.')
            saved = save_orders(conn, vendor, orders, day, day, clear_error=False,
                                remaining_dates=outside_pending + planned[index + 1:])
        except Exception as cause:
            result['coverage'] = coverage_status(conn, vendor, start, end)
            error = CollectionError(day, result, cause)
            try:
                with conn:
                    conn.executemany('INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)', [
                        ('rg_orders_last_error:' + vendor, str(error)[:2000]),
                        ('rg_orders_failed_date:' + vendor, day),
                        ('rg_orders_remaining_dates:' + vendor, ','.join(outside_pending + result['remaining_dates'])),
                        ('rg_orders_last_result:' + vendor, summary(result))])
            except Exception:
                # A locked/full database must not hide the original fetch/save error.
                pass
            raise error from cause
        for key in ('orders', 'inserted', 'updated', 'unchanged', 'conflicts', 'unmapped'):
            result[key] += saved[key]
        result['completed_dates'].append(day)
        result['remaining_dates'] = planned[index + 1:]
        if on_saved:
            on_saved(day)
    result['coverage'] = coverage_status(conn, vendor, start, end)
    pending_error = (f'이전 수집의 미완료 날짜 {len(outside_pending)}일 · 해당 기간을 조회하면 이어받습니다.'
                     if outside_pending else '')
    with conn:
        conn.executemany('INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)', [
            ('rg_orders_last_error:' + vendor, pending_error),
            ('rg_orders_failed_date:' + vendor, outside_pending[0] if outside_pending else ''),
            ('rg_orders_remaining_dates:' + vendor, ','.join(outside_pending)),
            ('rg_orders_last_result:' + vendor, summary(result))])
    return result


def summary(result):
    prefix = ''
    if 'completed_dates' in result:
        prefix = (f"이번 실행 {len(result['completed_dates'])}일 저장"
                  f" · 기존 수집 {len(result['skipped_dates'])}일 재사용"
                  f" · 남은 조회 {len(result['remaining_dates'])}일 · ")
    prefix += f"주문 {result['orders']:,}건 수집 · " if 'orders' in result else ''
    return (prefix + f"일별 옵션 매출 추가 {result['inserted']} / 갱신 {result['updated']} / 동일 {result['unchanged']}"
            f" · 기존 기록 우선 {result['conflicts']} · 모델 미연결 {result['unmapped']}")


def source_label(source):
    if str(source or '').startswith(SOURCE_PREFIX):
        return '주문 API · 출고분·잠정'
    return {'coupang-insights': '쿠팡 매출 Excel', 'manual': '직접 입력', 'csv': 'CSV',
            'excel': '기존 Excel'}.get(source, '기존 자료')
