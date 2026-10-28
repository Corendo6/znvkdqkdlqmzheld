"""Rocket Growth shipped orders, recorded by their Korean payment date."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

KST = timezone(timedelta(hours=9))
SOURCE_PREFIX = 'rg-order-api:'
ORDER_PATH = '/v2/providers/rg_open_api/apis/api/v1/vendors/{vendorId}/rg/orders'


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


def fetch_orders(api, start, end, progress=None):
    first, last = validate_period(start, end)
    params = {'paidDateFrom': first.strftime('%Y%m%d'), 'paidDateTo': last.strftime('%Y%m%d')}
    path = ORDER_PATH.format(vendorId=api.vendor_id)
    orders, seen_tokens = {}, set()
    for page in range(1, 10001):
        api.check_cancelled()
        api.phase = f'로켓그로스 일일 매출 {page}페이지'
        response = api._request('GET', path, params)
        if not isinstance(response, dict) or str(response.get('code')).upper() not in ('200', 'SUCCESS'):
            raise ValueError('주문 조회 실패: ' + str(response.get('message', '') if isinstance(response, dict) else '응답 형식 오류'))
        if not isinstance(response.get('data'), list):
            raise ValueError('주문 목록이 없는 응답입니다. 이전 매출을 유지합니다.')
        for raw in response['data']:
            order = normalize_order(raw, api.vendor_id, first.isoformat(), last.isoformat())
            old = orders.get(order['order_id'])
            if old is not None and old != order:
                raise ValueError('페이지 사이에서 같은 주문 내용이 달라졌습니다. 다시 조회하세요.')
            orders[order['order_id']] = order
        if progress:
            progress(f'로켓그로스 일일 매출 {page}페이지 · {len(orders):,}개 주문 수신')
        token = response.get('nextToken')
        if token in (None, ''):
            api.check_cancelled()
            return list(orders.values())
        token = str(token)
        if token in seen_tokens:
            raise ValueError('주문 조회 페이지가 반복되었습니다. 이전 매출을 유지합니다.')
        seen_tokens.add(token)
        params['nextToken'] = token
    raise ValueError('주문 조회 페이지 수가 너무 많습니다. 기간을 줄여 다시 조회하세요.')


def prefer_external_sale(conn, paid_date, model, method, option_id=''):
    """A user/Excel total takes precedence over API-derived rows, never over raw orders."""
    if method != '로켓그로스':
        return
    if option_id:
        where, args = 'option_id=?', [str(option_id)]
    else:
        where, args = "(model=? OR COALESCE(model,'')='')", [model]
    conn.execute(f"DELETE FROM sales WHERE sale_date=? AND method='로켓그로스' AND source LIKE ? AND {where}",
                 [paid_date, SOURCE_PREFIX + '%', *args])


def rebuild_sales(conn, vendor, start=None, end=None, option_ids=None):
    """Rebuild owned daily totals only. Caller owns the transaction."""
    source = SOURCE_PREFIX + vendor
    where, params = 'vendor_id=?', [vendor]
    sale_where, sale_params = 'source=?', [source]
    if start is not None and end is not None:
        where += ' AND paid_date BETWEEN ? AND ?'
        params += [start, end]
        sale_where += ' AND sale_date BETWEEN ? AND ?'
        sale_params += [start, end]
    if option_ids is not None:
        if not option_ids:
            return dict(inserted=0, updated=0, unchanged=0, conflicts=0, unmapped=0)
        marks = ','.join('?' for _ in option_ids)
        where += f' AND option_id IN ({marks})'
        sale_where += f' AND option_id IN ({marks})'
        params += list(option_ids)
        sale_params += list(option_ids)
    raw = conn.execute('SELECT * FROM rg_order_items WHERE ' + where, params).fetchall()
    current = conn.execute('SELECT * FROM sales WHERE ' + sale_where, sale_params).fetchall()
    grouped, owned = {}, {}
    for row in raw:
        key = (row['paid_date'], row['option_id'])
        group = grouped.setdefault(key, dict(quantity=0, revenue=Decimal(0), orders=set(), product=row['product_name']))
        group['quantity'] += row['quantity']
        group['revenue'] += Decimal(row['revenue'])
        group['orders'].add(row['order_id'])
    for row in current:
        owned.setdefault((row['sale_date'], row['option_id']), []).append(row)
    mappings = {r['option_id']: r['model'] for r in conn.execute('SELECT option_id,model FROM option_model_map') if r['model']}
    products = {}
    for r in conn.execute("SELECT * FROM products WHERE fulfillment_type='로켓그로스' ORDER BY updated_at DESC,id DESC"):
        products.setdefault(str(r['option_id'] or ''), dict(r))
    # Index externally maintained totals once, not one SQL scan for every option.
    external = {}
    for r in conn.execute("SELECT sale_date,option_id,model FROM sales WHERE method='로켓그로스' AND COALESCE(source,'')<>?", (source,)):
        external.setdefault(r['sale_date'], []).append((str(r['option_id'] or ''), str(r['model'] or '')))
    pending = {(r['sale_date'], r['option_id']) for r in conn.execute("SELECT sale_date,option_id FROM sales_pending WHERE method='로켓그로스'")}
    result = dict(inserted=0, updated=0, unchanged=0, conflicts=0, unmapped=0)
    for key in sorted(set(grouped) | set(owned)):
        paid, oid = key
        rows = owned.get(key, [])
        group = grouped.get(key)
        product = products.get(oid, {})
        model = str(mappings.get(oid) or product.get('model') or '').strip()
        conflict = key in pending or any(other_oid == oid or (not other_oid and (not model or other_model == model))
                                        for other_oid, other_model in external.get(paid, []))
        if group is None or conflict:
            if rows:
                conn.executemany('DELETE FROM sales WHERE id=?', [(r['id'],) for r in rows])
            if group is not None:
                result['conflicts'] += 1
            continue
        name = str(product.get('option_name') or group['product'] or '')
        title = str(product.get('product_name') or group['product'] or '')
        registered = str(product.get('registered_product_id') or '')
        values = (model, '로켓그로스', float(group['revenue']), len(group['orders']), group['quantity'],
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
            conn.executemany('DELETE FROM sales WHERE id=?', [(r['id'],) for r in rows[1:]])
        else:
            conn.execute('''INSERT INTO sales(sale_date,model,method,revenue,orders,quantity,option_id,
                option_name,product_name,registered_product_id,source) VALUES (?,?,?,?,?,?,?,?,?,?,?)''', (paid,) + values)
            result['inserted'] += 1
        if not model:
            result['unmapped'] += 1
            conn.execute('''INSERT INTO unmatched_options(option_id,option_name,product_name,registered_product_id,method)
                VALUES (?,?,?,?,'로켓그로스') ON CONFLICT(option_id) DO UPDATE SET
                option_name=excluded.option_name,product_name=excluded.product_name,last_seen=CURRENT_TIMESTAMP''',
                         (oid, name, title, registered))
        else:
            conn.execute('DELETE FROM unmatched_options WHERE option_id=?', (oid,))
    return result


def save_orders(conn, vendor, orders, start, end):
    """Commit only after every page succeeds. Repeated queries replace the same orders."""
    validate_period(start, end)
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
        result['orders'] = len(orders)
        for key, value in [('last_sync', stamp), ('last_from', start), ('last_to', end), ('last_error', '')]:
            conn.execute('INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)', ('rg_orders_' + key + ':' + vendor, value))
        conn.execute('INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)',
                     ('rg_orders_last_result:' + vendor, summary(result)))
    return result


def summary(result):
    return (f"매출 추가 {result['inserted']} / 갱신 {result['updated']} / 동일 {result['unchanged']}"
            f" · 기존 기록 우선 {result['conflicts']} · 모델 미연결 {result['unmapped']}")


def source_label(source):
    if str(source or '').startswith(SOURCE_PREFIX):
        return '주문 API · 출고분·잠정'
    return {'coupang-insights': '쿠팡 매출 Excel', 'manual': '직접 입력', 'csv': 'CSV',
            'excel': '기존 Excel'}.get(source, '기존 자료')
