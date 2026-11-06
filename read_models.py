"""Shared read-only SQLite projections, scoped to a connection and its revision.

Raw API orders stay separate from the editable sales ledger. Cached values are
immutable and never include an uncommitted transaction or a failed read.
"""
from collections import OrderedDict
from decimal import Decimal
from threading import RLock
from types import MappingProxyType


_CACHE = OrderedDict()
_LOCK = RLock()
_MAX_CONNECTIONS = 8
_MAX_ENTRIES = 12


def clear(conn):
    """Release a connection's cached projections when its database is closed."""
    with _LOCK:
        _CACHE.pop(conn, None)


def _revision(conn):
    # total_changes covers this connection; data_version covers other writers.
    # schema_version also detects DDL, which does not increment total_changes.
    return (conn.total_changes, conn.execute('PRAGMA data_version').fetchone()[0],
            conn.execute('PRAGMA schema_version').fetchone()[0])


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(value)
    return value


def cached_read(conn, key, loader):
    """Read an immutable projection; active transactions always bypass the cache.

    Clearing on transaction entry also prevents rollback from exposing a cached
    uncommitted result. The bounded connection keys cannot collide after close.
    """
    if conn.in_transaction:
        with _LOCK:
            _CACHE.pop(conn, None)
        return _freeze(loader())
    revision = _revision(conn)
    with _LOCK:
        entry = _CACHE.get(conn)
        if entry is None or entry[0] != revision:
            entry = (revision, OrderedDict())
            _CACHE[conn] = entry
        _CACHE.move_to_end(conn)
        while len(_CACHE) > _MAX_CONNECTIONS:
            _CACHE.popitem(last=False)
        if key in entry[1]:
            entry[1].move_to_end(key)
            return entry[1][key]
    value = _freeze(loader())
    # A concurrent writer may commit between SELECTs. Do not retain that read.
    if not conn.in_transaction and _revision(conn) == revision:
        with _LOCK:
            if _CACHE.get(conn) is entry:
                entry[1][key] = value
                while len(entry[1]) > _MAX_ENTRIES:
                    entry[1].popitem(last=False)
    return value


def option_models(conn):
    """Stored mappings, before consumer-specific model precedence/normalization."""
    return cached_read(conn, 'option_models', lambda: {
        str(row['option_id'] or ''): row['model']
        for row in conn.execute('SELECT option_id,model FROM option_model_map')})


def catalog_snapshot(conn):
    """Newest registered Rocket Growth row per option, plus stored mappings."""
    def load():
        products = {}
        for row in conn.execute("SELECT * FROM products WHERE fulfillment_type='로켓그로스' "
                                'ORDER BY updated_at DESC,id DESC'):
            products.setdefault(str(row['option_id'] or ''), dict(row))
        return dict(products=products, model_map=dict(option_models(conn)))
    return cached_read(conn, 'catalog', load)


def daily_api_totals(conn, vendor, start=None, end=None, option_ids=None):
    """One exact aggregate per (payment day, option), never editable sales rows."""
    options = None if option_ids is None else tuple(sorted(set(option_ids)))
    key = ('daily_api_totals', vendor, start, end, options)
    def load():
        where, params = 'vendor_id=?', [vendor]
        if start is not None and end is not None:
            where += ' AND paid_date BETWEEN ? AND ?'
            params.extend((start, end))
        if options is not None:
            if not options:
                return {}
            where += ' AND option_id IN (' + ','.join('?' for _ in options) + ')'
            params.extend(options)
        groups = {}
        for row in conn.execute('SELECT paid_date,option_id,quantity,revenue,product_name '
                                'FROM rg_order_items WHERE ' + where, params):
            group_key = (row['paid_date'], row['option_id'])
            group = groups.get(group_key)
            if group is None:
                group = groups[group_key] = dict(quantity=0, revenue=Decimal(0), orders=0,
                                                product=row['product_name'])
            group['quantity'] += row['quantity']
            group['revenue'] += Decimal(row['revenue'])
            # The primary key permits one row per vendor/order/option.
            group['orders'] += 1
        return groups
    return cached_read(conn, key, load)
