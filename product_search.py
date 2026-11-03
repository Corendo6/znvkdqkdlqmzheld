"""The same literal product-field search for SQLite and in-memory views."""
import re


FIELDS = ('전체', '노출상품ID', '옵션ID', '등록상품ID', '모델명', '등록상품명', '노출상품명')
FIELD_COLUMNS = {
    '노출상품ID': 'exposed_product_id',
    '옵션ID': 'option_id',
    '등록상품ID': 'registered_product_id',
    '모델명': 'model',
    '등록상품명': 'product_name',
    '노출상품명': 'display_name',
}
ALL_COLUMNS = tuple(FIELD_COLUMNS.values()) + ('option_name', 'seller_product_item_id')
ID_COLUMNS = frozenset(('exposed_product_id', 'option_id', 'registered_product_id',
                        'seller_product_item_id'))
_ASCII_LOWER = str.maketrans('ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')


def normalize_field(field):
    return field if field in FIELDS else '전체'


def id_term(query):
    """Permit copied numeric separators without extracting digits from product names."""
    text = str(query or '').strip()
    if re.fullmatch(r'\d(?:[\d\s,\-]*\d)?', text):
        return re.sub(r'[\s,\-]', '', text)
    return text


def _selected_columns(field, columns=None):
    field = normalize_field(field)
    selected = ALL_COLUMNS if field == '전체' else (FIELD_COLUMNS[field],)
    available = set(ALL_COLUMNS if columns is None else columns)
    return tuple(column for column in selected if column in available)


def matches(row, query, field='전체'):
    """Match a dict/SQLite row without joining fields or interpreting wildcards."""
    query = str(query or '').strip()
    if not query:
        return True
    # SQLite LIKE folds ASCII case; Korean text remains a literal substring.
    text_term = query.translate(_ASCII_LOWER)
    numeric_term = id_term(query).translate(_ASCII_LOWER)
    for column in _selected_columns(field):
        try:
            value = row[column]
        except (KeyError, IndexError):
            continue
        haystack = str(value if value is not None else '').translate(_ASCII_LOWER)
        if (numeric_term if column in ID_COLUMNS else text_term) in haystack:
            return True
    return False


def _escape_like(text):
    return text.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


def sql_clause(query, field='전체', alias='p', columns=None):
    """Return a parameterized predicate using only the known product columns."""
    if alias and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', alias):
        raise ValueError('Invalid SQL table alias')
    query = str(query or '').strip()
    if not query:
        return '1', []
    selected = _selected_columns(field, columns)
    if not selected:
        return '0', []
    prefix = alias + '.' if alias else ''
    clauses, params = [], []
    for column in selected:
        clauses.append(f"COALESCE({prefix}{column},'') LIKE ? ESCAPE '\\'")
        term = id_term(query) if column in ID_COLUMNS else query
        params.append('%' + _escape_like(term) + '%')
    return '(' + ' OR '.join(clauses) + ')', params
