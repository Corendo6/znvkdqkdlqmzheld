# -*- coding: utf-8 -*-
"""Rocket Growth inventory, replenishment estimates, and WING-like desktop page.

Only the dedicated RG API supplies orderable stock / trailing 30-day sales.
Missing observations remain unknown; inbound quantities are explicitly manual.
"""
import csv
import gross_margin
import hashlib
import io
import math
import queue
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
from collections import OrderedDict
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, font as tkfont

KST = timezone(timedelta(hours=9))
INVENTORY_PATH = '/v2/providers/rg_open_api/apis/api/v1/vendors/{vendorId}/rg/inventory/summaries'
BG, INK, MUTED, BLUE = '#ffffff', '#172b4d', '#8293a8', '#346bdb'
BORDER, PALE, ORANGE, GREEN = '#dce2e9', '#eef8fa', '#e87d15', '#20964f'


def now_kst():
    return datetime.now(KST)


def ensure_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS rg_inventory_snapshots(
        vendor_id TEXT NOT NULL, option_id TEXT NOT NULL, external_sku_id TEXT,
        orderable_quantity INTEGER, sales_30 INTEGER, fetched_at TEXT NOT NULL,
        in_latest_response INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY(vendor_id,option_id))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS rg_replenishment(
        vendor_id TEXT NOT NULL, option_id TEXT NOT NULL,
        pending_quantity INTEGER CHECK(pending_quantity >= 0),
        pack_quantity INTEGER NOT NULL DEFAULT 1 CHECK(pack_quantity >= 1),
        note TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
        PRIMARY KEY(vendor_id,option_id))''')
    cols = {r[1] for r in conn.execute('PRAGMA table_info(products)')}
    if 'image_url' not in cols:
        conn.execute('ALTER TABLE products ADD COLUMN image_url TEXT')


def whole_number(value, name='수량', nullable=True):
    if value is None or value == '':
        if nullable:
            return None
        raise ValueError(name + '을 입력하세요.')
    if isinstance(value, bool):
        raise ValueError(name + '은 0 이상의 정수여야 합니다.')
    try:
        number = Decimal(str(value).replace(',', '').strip())
        if not number.is_finite() or number < 0 or number != number.to_integral_value():
            raise ValueError()
        return int(number)
    except Exception:
        raise ValueError(name + '은 0 이상의 정수여야 합니다.') from None


def fetch_inventory(api, progress=None, vendor_item_id=None):
    """Fetch all pages without accepting a partial / malformed snapshot."""
    path = INVENTORY_PATH.format(vendorId=urllib.parse.quote(api.vendor_id, safe=''))
    token, seen, collected = None, set(), {}
    for page in range(1, 10001):
        params = []
        if vendor_item_id:
            params.append(('vendorItemId', str(vendor_item_id)))
        elif token is not None:
            params.append(('nextToken', token))
        result = api._request('GET', path, params)
        if not isinstance(result, dict) or str(result.get('code', '')).upper() not in ('200', 'SUCCESS'):
            raise ValueError((result.get('message') if isinstance(result, dict) else '') or '재고 API 조회에 실패했습니다.')
        rows = result.get('data')
        if not isinstance(rows, list):
            raise ValueError('재고 목록이 누락되었거나 올바른 형식이 아닙니다. 이전 조회 결과를 유지합니다.')
        for item in rows:
            if not isinstance(item, dict):
                raise ValueError('재고 응답의 상품 정보가 올바르지 않습니다.')
            if str(item.get('vendorId') or '') != api.vendor_id:
                raise ValueError('재고 응답의 판매자 번호가 요청한 판매자와 다릅니다.')
            oid = str(item.get('vendorItemId') or '').strip()
            if not oid or (vendor_item_id and oid != str(vendor_item_id)):
                raise ValueError('재고 응답의 옵션 번호가 올바르지 않습니다.')
            inventory = item.get('inventoryDetails')
            sales = item.get('salesCountMap')
            if inventory is not None and not isinstance(inventory, dict):
                raise ValueError('재고 상세 응답 형식이 올바르지 않습니다.')
            if sales is not None and not isinstance(sales, dict):
                raise ValueError('판매량 응답 형식이 올바르지 않습니다.')
            row = dict(option_id=oid, external_sku_id=str(item.get('externalSkuId') or ''),
                       orderable_quantity=whole_number((inventory or {}).get('totalOrderableQuantity')),
                       sales_30=whole_number((sales or {}).get('SALES_COUNT_LAST_THIRTY_DAYS')))
            if oid in collected and collected[oid] != row:
                raise ValueError('같은 옵션의 재고가 중복 응답에서 서로 다릅니다. 다시 조회해 주세요.')
            collected[oid] = row
        if progress:
            progress(f'로켓그로스 재고 {page}페이지 · {len(collected):,}개 옵션 확인')
        next_token = result.get('nextToken')
        if vendor_item_id or next_token in (None, '', 0, '0'):
            return list(collected.values())
        token = str(next_token)
        if token in seen:
            raise ValueError('재고 목록의 다음 페이지가 반복됩니다. 이전 조회 결과를 유지합니다.')
        seen.add(token)
    raise ValueError('재고 목록 페이지가 너무 많습니다. 이전 조회 결과를 유지합니다.')


def save_snapshot(conn, vendor, rows, stamp=None):
    """Replace the membership only after a complete, validated fetch; never infer 0."""
    stamp = stamp or now_kst().isoformat(timespec='seconds')
    checked = [(vendor, r['option_id'], r.get('external_sku_id', ''),
                whole_number(r.get('orderable_quantity')), whole_number(r.get('sales_30')), stamp)
               for r in rows]
    with conn:
        conn.execute('UPDATE rg_inventory_snapshots SET in_latest_response=0 WHERE vendor_id=?', (vendor,))
        conn.executemany('''INSERT INTO rg_inventory_snapshots
            (vendor_id,option_id,external_sku_id,orderable_quantity,sales_30,fetched_at,in_latest_response)
            VALUES (?,?,?,?,?,?,1) ON CONFLICT(vendor_id,option_id) DO UPDATE SET
            external_sku_id=excluded.external_sku_id,orderable_quantity=excluded.orderable_quantity,
            sales_30=excluded.sales_30,fetched_at=excluded.fetched_at,in_latest_response=1''', checked)
        conn.execute('INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)', ('rg_last_sync:' + vendor, stamp))
        conn.execute('DELETE FROM settings WHERE key=?', ('rg_last_error:' + vendor,))
    return stamp


def replenishment(stock, sales_30, pending, lead=5, target=14, safety=3, pack=1):
    """Return a unit-rounded estimate, or None if a required observation is unknown."""
    if stock is None or sales_30 is None or pending is None or sales_30 == 0:
        return None
    stock, sales_30, pending = [whole_number(v, nullable=False) for v in (stock, sales_30, pending)]
    lead, target, safety = [whole_number(v, nullable=False) for v in (lead, target, safety)]
    pack = whole_number(pack, nullable=False)
    if pack < 1:
        raise ValueError('입고 단위는 1 이상이어야 합니다.')
    needed = Decimal(sales_30) * (lead + target + safety) / Decimal(30) - stock - pending
    return max(0, int((needed / pack).to_integral_value(rounding=ROUND_CEILING)) * pack)


def inventory_state(stock, sales_30, fetched_at, present=True, lead=5, safety=3, now=None):
    if not fetched_at:
        return '조회 전', None, False
    if not present:
        return '응답 없음', None, False
    try:
        stamp = datetime.fromisoformat(fetched_at)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=KST)
        stale = (now or now_kst()) - stamp > timedelta(hours=24)
    except (ValueError, TypeError):
        stale = True
    if stale:
        return '재조회 필요', None, False
    if stock is None:
        return '재고 미확인', None, False
    if stock == 0:
        return '품절', 0.0, True
    if sales_30 is None or sales_30 <= 0:
        return '판매량 확인', None, True
    days = stock * 30 / sales_30
    return ('품절 임박' if days <= lead + safety else '양호'), days, True


def product_image_url(item):
    images = [x for x in (item.get('images') or []) if isinstance(x, dict)]
    images.sort(key=lambda x: (x.get('imageType') != 'REPRESENTATION', str(x.get('imageOrder', '0'))))
    for image in images:
        for key in ('cdnPath', 'vendorPath'):
            url = str(image.get(key) or '').strip()
            if url.startswith(('https://', 'http://')):
                return url
            if url.startswith('//'):
                return 'https:' + url
            if key == 'cdnPath' and url.startswith('vendor_inventory/'):
                return 'https://image.coupangcdn.com/image/' + url
    return ''


def estimated_costs(app, products):
    """Compatibility helper: fees use the same batch context as every margin view."""
    context = gross_margin.load_context(app.db)
    return {p.get('id'): result['estimated_cost'] for p in products
            if (result := gross_margin.evaluate(p, context, allow_reference_price=False))['estimated_cost'] is not None}


def build_rows(app, now=None):
    now = now or now_kst()
    vendor = app.db.setting('api_vendor_id', '').strip()
    lead = whole_number(app.db.setting('rg_lead_days', '5'), nullable=False)
    target = whole_number(app.db.setting('rg_target_days', '14'), nullable=False)
    safety = whole_number(app.db.setting('rg_safety_days', '3'), nullable=False)
    pending = {r['option_id']: dict(r) for r in app.db.query('SELECT * FROM rg_replenishment WHERE vendor_id=?', (vendor,))}
    # Catalog enrollment alone is not evidence that an option is in RG inventory.
    # Keep recent sold-out options; hide confirmed 0 stock / 0 sales / no inbound.
    snapshots = {r['option_id']: dict(r) for r in app.db.query(
        'SELECT * FROM rg_inventory_snapshots WHERE vendor_id=? AND in_latest_response=1', (vendor,))
        if not (r['orderable_quantity'] == 0 and r['sales_30'] == 0
                and not pending.get(r['option_id'], {}).get('pending_quantity'))}
    products = {}
    for row in app.db.query("SELECT * FROM products WHERE fulfillment_type='로켓그로스' ORDER BY updated_at DESC,id DESC"):
        p = dict(row)
        oid = str(p.get('option_id') or '').strip()
        if oid in snapshots and oid not in products:
            products[oid] = p
    for oid, snapshot in snapshots.items():
        if oid not in products:
            products[oid] = dict(option_id=oid, product_name='상품정보 연결 필요', model='', id=None)
    yesterday = now.date() - timedelta(days=1)
    week_start = now.date() - timedelta(days=7)
    stored = {}
    for row in app.db.query('''SELECT option_id,sale_date,SUM(quantity) quantity FROM sales
            WHERE method='로켓그로스' AND COALESCE(option_id,'')<>'' AND sale_date BETWEEN ? AND ?
            GROUP BY option_id,sale_date''', (week_start.isoformat(), yesterday.isoformat())):
        stored.setdefault(str(row['option_id']), {})[row['sale_date']] = row['quantity']
    context = gross_margin.load_context(app.db)
    rows = []
    for key, p in products.items():
        gross = gross_margin.evaluate(p, context, allow_reference_price=False)
        p = dict(p, model=gross['model'])
        oid = str(p.get('option_id') or '').strip()
        snap, manual = snapshots.get(oid, {}), pending.get(oid, {})
        stock, sales = snap.get('orderable_quantity'), snap.get('sales_30')
        state, days, usable = inventory_state(stock, sales, snap.get('fetched_at'),
                                              bool(snap.get('in_latest_response')), lead, safety, now)
        quantity = manual.get('pending_quantity')
        pack = manual.get('pack_quantity', 1)
        recommended = replenishment(stock, sales, quantity, lead, target, safety, pack) if usable else None
        history = stored.get(oid, {})
        rows.append(dict(p, key=key, vendor_id=vendor, stock=stock, sales_30=sales,
                         pending=quantity, pack=pack, pending_note=manual.get('note', ''),
                         pending_updated=manual.get('updated_at'), recommended=recommended,
                         state=state, days=days, usable=usable, checked_at=snap.get('fetched_at'),
                         external_sku_id=snap.get('external_sku_id', ''),
                         yesterday=history.get(yesterday.isoformat()),
                         last7=sum(history.values()) if history else None, recorded_days=len(history),
                         gross=gross, estimated_cost=gross['estimated_cost'],
                         unit_cost=gross['cost'], logistics=gross['logistics'],
                         margin=gross['margin'], margin_rate=gross['margin_rate'],
                         margin_missing=', '.join(gross['missing']),
                         search_text=' '.join(str(p.get(k) or '') for k in ('model', 'product_name', 'display_name',
                             'option_name', 'option_id', 'registered_product_id', 'exposed_product_id')).casefold()))
    return rows


def format_number(value):
    return '—' if value is None else f'{value:,.0f}'


def save_pending(conn, vendor, option_ids, quantity, pack=1, note=''):
    if not vendor:
        raise ValueError('데이터 · 설정에서 판매자 번호를 먼저 저장하세요.')
    quantity = whole_number(quantity, '입고중 수량', nullable=False)
    pack = whole_number(pack, '입고 단위', nullable=False)
    if pack < 1:
        raise ValueError('입고 단위는 1 이상이어야 합니다.')
    stamp = now_kst().isoformat(timespec='seconds')
    with conn:
        for oid in option_ids:
            if not oid or str(oid).startswith('unmapped:'):
                raise ValueError('옵션 번호가 없는 상품은 상품정보를 먼저 연결하세요.')
            conn.execute('''INSERT INTO rg_replenishment
                    (vendor_id,option_id,pending_quantity,pack_quantity,note,updated_at) VALUES (?,?,?,?,?,?)
                    ON CONFLICT(vendor_id,option_id) DO UPDATE SET pending_quantity=excluded.pending_quantity,
                    pack_quantity=excluded.pack_quantity,note=excluded.note,updated_at=excluded.updated_at''',
                         (vendor, oid, quantity, pack, note, stamp))


class ThumbnailCache:
    """App lifetime, 3 workers, bounded queue/cache. Tk objects only on the UI thread."""
    WORKERS, MAX_PENDING, MAX_CACHED = 3, 18, 128

    def __init__(self, app):
        self.app = app
        self.directory = Path(app.db.path).parent / 'rg_thumbnails'
        self.directory.mkdir(exist_ok=True)
        self.tasks = queue.Queue(maxsize=self.MAX_PENDING)
        self.results = queue.Queue(maxsize=self.MAX_PENDING)
        self.pending = set()
        self.cache, self.failed = OrderedDict(), OrderedDict()
        self.stop = threading.Event()
        self.workers = []

    def get(self, url):
        if url in self.cache:
            self.cache.move_to_end(url)
            return self.cache[url]
        if not url.startswith(('https://', 'http://')) or self.stop.is_set():
            return None
        if url in self.failed and time.monotonic()-self.failed[url] < 600:
            return None
        if url not in self.pending and len(self.pending) < self.MAX_PENDING:
            if not self.workers:
                for _ in range(self.WORKERS):
                    worker = threading.Thread(target=self._worker, daemon=True, name='RGThumbnail')
                    self.workers.append(worker)
                    worker.start()
            self.pending.add(url)
            self.tasks.put_nowait(url)
        return None

    def _load(self, url):
        from PIL import Image, ImageOps
        cached = self.directory / (hashlib.sha256(url.encode()).hexdigest() + '.png')
        if cached.exists():
            try:
                with Image.open(cached) as source:
                    return ImageOps.contain(source.convert('RGB'), (46, 46))
            except (OSError, ValueError):
                pass
        request = urllib.request.Request(url, headers={'User-Agent': 'CoupangManager/0.4.4'})
        with urllib.request.urlopen(request, timeout=8) as response:
            raw = response.read(4*1024*1024+1)
        if len(raw) > 4*1024*1024:
            raise ValueError('대표이미지가 너무 큽니다.')
        with Image.open(io.BytesIO(raw)) as source:
            source.thumbnail((46, 46))
            image = ImageOps.contain(source.convert('RGB'), (46, 46))
        try:
            image.save(cached)
        except OSError:
            pass
        return image

    def _worker(self):
        while not self.stop.is_set():
            try:
                url = self.tasks.get(timeout=.2)
            except queue.Empty:
                continue
            try:
                image = self._load(url) if not self.stop.is_set() else None
            except Exception:
                image = None
            if not self.stop.is_set():
                self.results.put_nowait((url, image))
            self.tasks.task_done()

    def drain(self):
        changed = set()
        # Never let image completions monopolize the UI loop.
        for _ in range(6):
            try:
                url, image = self.results.get_nowait()
            except queue.Empty:
                break
            self.pending.discard(url)
            changed.add(url)
            if image is None:
                self.failed[url] = time.monotonic()
                if len(self.failed) > self.MAX_CACHED:
                    self.failed.popitem(last=False)
                continue
            from PIL import ImageTk
            self.cache[url] = ImageTk.PhotoImage(image, master=self.app)
            self.cache.move_to_end(url)
            if len(self.cache) > self.MAX_CACHED:
                self.cache.popitem(last=False)
        return changed

    def close(self):
        self.stop.set()
        self.cache.clear()


class RocketInventoryPage(tk.Frame):
    WIDTHS = (34, 260, 108, 144, 138, 130, 110)
    ROW_HEIGHT = 132

    def __init__(self, parent, app):
        super().__init__(parent, bg=BG)
        self.app = app
        self.font = '맑은 고딕' if __import__('os').name == 'nt' else 'Noto Sans CJK KR'
        self.page_number = 1
        self.selected = set()
        self.visible_rows = []
        self._drawn_rows, self._photos, self._fonts, self._text_cache = {}, {}, {}, {}
        self._draw_signature = None
        self._generation = 0
        self._width = sum(self.WIDTHS)
        self._focus_index = 0
        self.draw_job = None
        if not hasattr(app, 'rg_thumbnails'):
            app.rg_thumbnails = ThumbnailCache(app)
        self.thumbnails = app.rg_thumbnails
        self.after_job = None
        self.search_job = None
        self.search = tk.StringVar()
        self.filter = tk.StringVar(value='전체 상태')
        self.sort = tk.StringVar(value='상품명 순')
        self.page_size = tk.StringVar(value='20')
        self.all_checked = tk.BooleanVar(value=False)
        self._build()
        self.refresh()
        self.after_job = self.after(150, self._poll_images)

    def destroy(self):
        for job in (self.after_job, self.search_job, self.draw_job):
            if job:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
        super().destroy()

    def label(self, parent, text='', color=INK, size=10, bold=False, **kw):
        return tk.Label(parent, text=text, bg=parent.cget('bg'), fg=color,
                        font=(self.font, size, 'bold' if bold else 'normal'), **kw)

    def button(self, parent, text, command, primary=False):
        return tk.Button(parent, text=text, command=command, relief='flat', bd=0,
                         bg=BLUE if primary else '#f1f4f8', fg=BG if primary else INK,
                         activebackground='#2458bf' if primary else '#e4eaf2',
                         activeforeground=BG if primary else INK,
                         font=(self.font, 10, 'bold' if primary else 'normal'),
                         padx=12, pady=7, cursor='hand2')

    def _build(self):
        title = tk.Frame(self, bg=BG)
        title.pack(fill='x', pady=(0, 6))
        self.label(title, '로켓그로스 재고 · 마진', size=21, bold=True).pack(side='left')
        self.label(title, 'API 갱신: 데이터 · 설정', color=MUTED, size=9).pack(side='right', padx=10)
        self.button(title, '입고 기준 설정', self.settings_dialog).pack(side='right', padx=8)
        self.status_label = self.label(self, '재고 API 조회 전', color=MUTED, anchor='w')
        self.status_label.pack(fill='x', pady=(0, 14))
        summary = tk.Frame(self, bg='#f6f9fc', padx=14, pady=11)
        summary.pack(fill='x', pady=(0, 12))
        self.summary_label = self.label(summary, '', size=11, bold=True, anchor='w')
        self.summary_label.pack(side='left')
        self.button(summary, '항목 안내', self.show_help).pack(side='right')
        toolbar = tk.Frame(self, bg=BG)
        toolbar.pack(fill='x', pady=(0, 10))
        self.button(toolbar, 'F5 검색', self.app.open_search_dialog).pack(side='left')
        self.search_label = self.label(toolbar, '', color=BLUE, size=9, anchor='w')
        self.search_label.pack(side='left', padx=(8, 0))
        self.clear_search_button = self.button(toolbar, '검색 해제', self.clear_search)
        cb = ttk.Combobox(toolbar, textvariable=self.filter, width=12, state='readonly',
                          values=['전체 상태', '입고 권장', '품절 임박', '품절', '양호', '미확인', '적자 상품', '마진 미확인'])
        cb.pack(side='left', padx=8, ipady=5)
        cb.bind('<<ComboboxSelected>>', lambda e: self.apply_filters())
        cb = ttk.Combobox(toolbar, textvariable=self.sort, width=13, state='readonly',
                          values=['상품명 순', '재고 적은 순', '판매량 많은 순', '권장 입고 많은 순', '마진 낮은 순'])
        cb.pack(side='left', ipady=5)
        cb.bind('<<ComboboxSelected>>', lambda e: self.apply_filters())
        self.button(toolbar, 'CSV 저장', self.export_csv).pack(side='right')
        self.button(toolbar, '선택 입고중 입력', self.edit_selected).pack(side='right', padx=8)
        self.label(self, '최근 재고 응답 상품  ·  현재 판매가 기준 마진 · 클릭: 비용 상세  ·  F5: 모델명 / 상품명 / 옵션 ID 검색', color=MUTED, size=9, anchor='w').pack(fill='x', pady=(0, 6))
        holder = tk.Frame(self, bg=BG, highlightthickness=1, highlightbackground=BORDER)
        holder.grid_columnconfigure(0, weight=1)
        holder.grid_rowconfigure(1, weight=1)
        self.header = tk.Canvas(holder, bg='#f8fafc', height=44, highlightthickness=0)
        self.header.grid(row=0, column=0, sticky='ew')
        self.canvas = tk.Canvas(holder, bg=BG, highlightthickness=0, yscrollincrement=32, takefocus=True)
        self.vbar = ttk.Scrollbar(holder, orient='vertical', command=self.canvas.yview)
        self.hbar = ttk.Scrollbar(holder, orient='horizontal', command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=self._yscroll, xscrollcommand=self._xscroll)
        self.vbar.grid(row=1, column=1, sticky='ns')
        self.hbar.grid(row=2, column=0, sticky='ew')
        self.canvas.grid(row=1, column=0, sticky='nsew')
        self.canvas.bind('<Configure>', self._resize)
        self.canvas.bind('<MouseWheel>', self._wheel)
        self.canvas.bind('<Button-4>', lambda e: self.canvas.yview_scroll(-3, 'units'))
        self.canvas.bind('<Button-5>', lambda e: self.canvas.yview_scroll(3, 'units'))
        self.canvas.bind('<Button-1>', self._click)
        self.canvas.bind('<Motion>', self._motion)
        self.header.bind('<Button-1>', self._header_click)
        self.canvas.bind('<Up>', lambda e: self._keyboard(-1))
        self.canvas.bind('<Down>', lambda e: self._keyboard(1))
        self.canvas.bind('<space>', self._keyboard_select)
        self.canvas.bind('<Return>', self._keyboard_edit)
        self.canvas.bind('<Control-Return>', self._keyboard_margin)
        footer = tk.Frame(self, bg=BG)
        footer.pack(side='bottom', fill='x', pady=(7, 0))
        self.selection_label = self.label(footer, '', color=MUTED)
        self.selection_label.pack(side='left')
        self.next_button = self.button(footer, '다음 ›', lambda: self.go_page(1))
        self.next_button.pack(side='right')
        self.pager_label = self.label(footer, '', width=14)
        self.pager_label.pack(side='right', padx=8)
        self.prev_button = self.button(footer, '‹ 이전', lambda: self.go_page(-1))
        self.prev_button.pack(side='right')
        cb = ttk.Combobox(footer, textvariable=self.page_size, values=['10', '20', '30', '50'], width=4, state='readonly')
        cb.pack(side='right', padx=(10, 20))
        cb.bind('<<ComboboxSelected>>', lambda e: self.apply_filters())
        self.label(footer, '페이지당').pack(side='right')
        self.label(self, '현재 판매가 기준 예상 마진 = 판매가 − 수수료(VAT) − 원가 − 물류비(VAT)  ·  보관·반품비 제외  ·  미확인 값은 —',
                   color=MUTED, size=9, anchor='w').pack(side='bottom', fill='x', pady=(6, 0))
        holder.pack(fill='both', expand=True)

    def clear_search(self):
        self.search.set('')
        self.apply_filters()

    def set_status(self, text):
        self.status_label.config(text=text)

    def _search_changed(self, *_):
        if self.search_job:
            self.after_cancel(self.search_job)
        self.search_job = self.after(250, self.apply_filters)

    def _resize(self, event):
        self._width = max(sum(self.WIDTHS), event.width)
        self.canvas.configure(scrollregion=(0, 0, self._width, len(self.visible_rows)*self.ROW_HEIGHT))
        self.header.configure(scrollregion=(0, 0, self._width, 44))
        self._draw_header()
        self._schedule_draw()

    def _yscroll(self, first, last):
        self.vbar.set(first, last)
        self._schedule_draw()

    def _xscroll(self, first, last):
        self.hbar.set(first, last)
        self.header.xview_moveto(first)

    def _schedule_draw(self):
        if self.draw_job is None:
            self.draw_job = self.after_idle(self._draw_viewport)

    def _wheel(self, event):
        delta = -1 if event.delta > 0 else 1
        self.canvas.yview_scroll(delta * 3, 'units')
        return 'break'

    def refresh(self):
        self.rows = build_rows(self.app)
        self.selected.intersection_update(r['key'] for r in self.rows)
        vendor = self.app.db.setting('api_vendor_id', '').strip()
        stamp = self.app.db.setting('rg_last_sync:' + vendor, '')
        error = self.app.db.setting('rg_last_error:' + vendor, '')
        status = '마지막 재고 조회: ' + (stamp.replace('T', ' ')[:19] + ' (한국시간)' if stamp else '아직 조회하지 않았습니다')
        if error:
            status += '  ·  최근 조회 실패 — 이전 결과 표시 중'
        self.set_status(status)
        total = len(self.rows)
        good = sum(r['state'] == '양호' for r in self.rows)
        low = sum(r['state'] == '품절 임박' for r in self.rows)
        empty = sum(r['state'] == '품절' for r in self.rows)
        recommended = sum((r['recommended'] or 0) > 0 for r in self.rows)
        self.summary_label.config(text=f'전체 {total:,}   ·   양호 {good:,}   ·   품절 임박 {low:,}   ·   품절 {empty:,}   ·   입고 권장 {recommended:,}')
        self.apply_filters(reset=False)

    def apply_filters(self, reset=True):
        if self.search_job:
            self.after_cancel(self.search_job)
        self.search_job = None
        if reset:
            self.page_number = 1
        query = self.search.get().strip().casefold()
        self.search_label.config(text=('검색: '+query[:10]+('…' if len(query)>10 else '')) if query else '')
        if query:
            self.clear_search_button.pack(side='left', after=self.search_label, padx=(0, 8))
        else:
            self.clear_search_button.pack_forget()
        kind = self.filter.get()
        rows = []
        for r in self.rows:
            if query and query not in r['search_text']:
                continue
            if kind == '입고 권장' and not (r['recommended'] is not None and r['recommended'] > 0):
                continue
            if kind == '미확인' and r['state'] in ('양호', '품절 임박', '품절'):
                continue
            if kind == '적자 상품' and not (r['margin'] is not None and r['margin'] < 0):
                continue
            if kind == '마진 미확인' and r['margin'] is not None:
                continue
            if kind not in ('전체 상태', '입고 권장', '미확인', '적자 상품', '마진 미확인') and kind != r['state']:
                continue
            rows.append(r)
        order = self.sort.get()
        if order == '마진 낮은 순':
            rows.sort(key=lambda r: (r['margin'] is None, r['margin'] or 0, r['key']))
        elif order == '재고 적은 순':
            rows.sort(key=lambda r: (r['stock'] is None, r['stock'] or 0, r['key']))
        elif order == '판매량 많은 순':
            rows.sort(key=lambda r: (r['sales_30'] is None, -(r['sales_30'] or 0), r['key']))
        elif order == '권장 입고 많은 순':
            rows.sort(key=lambda r: (r['recommended'] is None, -(r['recommended'] or 0), r['key']))
        else:
            rows.sort(key=lambda r: (str(r.get('product_name') or ''), str(r.get('option_name') or ''), r['key']))
        self.filtered_rows = rows
        self.render()

    def _columns(self):
        widths = list(self.WIDTHS)
        widths[1] += self._width - sum(widths)
        edges = [0]
        for width in widths:
            edges.append(edges[-1] + width)
        return edges

    def _font(self, size=10, bold=False):
        key = (size, bold)
        if key not in self._fonts:
            self._fonts[key] = tkfont.Font(self, family=self.font, size=size,
                                           weight='bold' if bold else 'normal')
        return self._fonts[key]

    def _text(self, canvas, x, y, text, color=INK, size=10, bold=False, anchor='nw', tags=()):
        return canvas.create_text(x, y, text=str(text), fill=color, font=self._font(size, bold),
                                  anchor=anchor, tags=tags, justify='left')

    def _fit(self, text, width, size=10, bold=False, lines=1):
        key = (text, width, size, bold, lines)
        if key in self._text_cache:
            return self._text_cache[key]
        remaining = ' '.join(str(text).split())
        font = self._font(size, bold)
        output = []
        for line in range(lines):
            if font.measure(remaining) <= width:
                output.append(remaining)
                break
            suffix = '…' if line == lines-1 else ''
            lo, hi = 0, len(remaining)
            while lo < hi:
                mid = (lo+hi+1)//2
                if font.measure(remaining[:mid]+suffix) <= width:
                    lo = mid
                else:
                    hi = mid-1
            output.append(remaining[:lo].rstrip()+suffix)
            remaining = remaining[lo:].lstrip()
        result = '\n'.join(output)
        if len(self._text_cache) > 2048:
            self._text_cache.clear()
        self._text_cache[key] = result
        return result

    def _checkbox(self, canvas, x, y, checked):
        canvas.create_rectangle(x, y, x+14, y+14, outline=BLUE if checked else '#aab6c5',
                                fill=BLUE if checked else BG)
        if checked:
            canvas.create_line(x+3, y+7, x+6, y+10, x+12, y+3, fill=BG, width=2)

    def _draw_header(self):
        self.header.delete('all')
        edges = self._columns()
        self._checkbox(self.header, 10, 15, self.all_checked.get())
        for col, title in enumerate(('상품정보', '판매량 (개)', '재고 (개)', '현재 판매가', '개당 예상 마진', '재고 상태'), 1):
            self._text(self.header, edges[col]+10, 13, title, color='#516b86')
        for x in edges[2:-1]:
            self.header.create_line(x, 0, x, 44, fill=BORDER)
        self.header.create_line(0, 43, self._width, 43, fill=BORDER)

    def render(self):
        size = int(self.page_size.get())
        pages = max(1, math.ceil(len(self.filtered_rows)/size))
        self.page_number = max(1, min(self.page_number, pages))
        start = (self.page_number-1)*size
        self.visible_rows = self.filtered_rows[start:start+size]
        self._focus_index = 0
        self.pager_label.config(text=f'{self.page_number} / {pages} 페이지')
        self.prev_button.config(state='normal' if self.page_number > 1 else 'disabled')
        self.next_button.config(state='normal' if self.page_number < pages else 'disabled')
        self.canvas.configure(scrollregion=(0, 0, self._width, len(self.visible_rows)*self.ROW_HEIGHT))
        self.canvas.yview_moveto(0)
        self._selection_changed()

    def _draw_viewport(self):
        self.draw_job = None
        top = max(0, self.canvas.canvasy(0))
        first = max(0, int(top//self.ROW_HEIGHT)-1)
        end = min(len(self.visible_rows), math.ceil((top+self.canvas.winfo_height())/self.ROW_HEIGHT)+1)
        signature = (first, end, self._width, self._generation)
        if signature == self._draw_signature:
            return
        self._draw_signature = signature
        self.canvas.delete('all')
        self._photos = {}
        self._drawn_rows = {index: self.visible_rows[index] for index in range(first, end)}
        if not self.visible_rows:
            text = ('조건에 맞는 상품이 없습니다.' if self.rows else
                    '표시할 로켓그로스 재고 상품이 없습니다.\n데이터 · 설정에서 API 갱신을 실행하세요.\n재고·30일 판매가 모두 0이고 입력된 입고중 물량도 없는 상품은 제외합니다.')
            self._text(self.canvas, 24, 65, text, color=MUTED)
        for index, row in self._drawn_rows.items():
            self._draw_row(index, row)

    def _draw_row(self, index, r):
        c, edges, y = self.canvas, self._columns(), index*self.ROW_HEIGHT
        selected = r['key'] in self.selected
        c.create_rectangle(0, y, self._width, y+self.ROW_HEIGHT,
                           fill='#f1f6fe' if selected else BG, outline='')
        c.create_line(0, y+self.ROW_HEIGHT-1, self._width, y+self.ROW_HEIGHT-1, fill=BORDER)
        for edge in edges[2:-1]:
            c.create_line(edge, y, edge, y+self.ROW_HEIGHT, fill='#e8edf3')
        self._checkbox(c, 10, y+23, selected)
        x = edges[1]+10
        url = str(r.get('image_url') or '')
        photo = self.thumbnails.get(url)
        if photo:
            self._photos[url] = photo
            c.create_image(x+23, y+41, image=photo)
        else:
            c.create_rectangle(x, y+18, x+46, y+64, fill='#f5f7fa', outline='')
            self._text(c, x+23, y+41, '이미지\n준비 중' if url and url not in self.thumbnails.failed else '이미지\n없음',
                       color='#a4b0bd', size=8, anchor='center')
        x += 56
        available = edges[2]-x-10
        title = r.get('product_name') or r.get('display_name') or '상품정보 연결 필요'
        self._text(c, x, y+15, self._fit(title, available, bold=True, lines=2), bold=True)
        self._text(c, x, y+61, self._fit(r.get('option_name') or '', available, size=9), color='#526a85', size=9)
        self._text(c, x, y+83, self._fit(r.get('model') or '모델 미연결', available, size=9), color=INK, size=9)
        self._text(c, x, y+106, self._fit(str(r.get('option_id') or '옵션 미연결')+' ↗', available, size=8),
                   color=BLUE, size=8, tags=('action:product', f'row:{index}'))
        def metric(col, offset, label, value, bold=False, bg=None, color=None):
            left, right = edges[col]+10, edges[col+1]-10
            if bg:
                c.create_rectangle(left-3, y+offset-3, right+3, y+offset+24, fill=bg, outline='')
            self._text(c, left, y+offset, label, color=INK if bold else MUTED, size=9)
            self._text(c, right, y+offset-1, value, color=color or (INK if bold else '#526a85'),
                       size=10, bold=bold, anchor='ne')
        metric(2, 20, '어제', format_number(r['yesterday']))
        metric(2, 48, '7일', format_number(r['last7']))
        metric(2, 76, '30일', format_number(r['sales_30']), True)
        self._text(c, edges[2]+10, y+107, f'7일 중 {r["recorded_days"]}일 기록', color=MUTED, size=8)
        metric(3, 20, '판매가능', format_number(r['stock']), True)
        text = '소진 예상 —' if r['days'] is None else f'소진 예상 {r["days"]:.1f}일'
        self._text(c, edges[3]+10, y+45, text, color=ORANGE if r['state'] in ('품절 임박', '품절') else MUTED, size=8)
        metric(3, 71, '입고중', format_number(r['pending']))
        metric(3, 100, '권장입고', format_number(r['recommended']), bg=PALE,
               color=BLUE if (r['recommended'] or 0) > 0 else MUTED)
        gross = r['gross']
        margin_tags = ('action:margin', f'row:{index}')
        c.create_rectangle(edges[4]+1, y+1, edges[6]-1, y+self.ROW_HEIGHT-2,
                           fill='', outline='', tags=margin_tags)
        self._text(c, edges[5]-10, y+17, format_number(gross['sale'])+' 원' if gross['sale'] is not None else '—',
                   color=BLUE, bold=True, size=12, anchor='ne', tags=margin_tags)
        metric(4, 61, '원가', format_number(gross['cost']))
        metric(4, 86, '물류비', format_number(gross['logistics']))
        self._text(c, edges[4]+10, y+109, '물류비 VAT 포함', color=MUTED, size=8)
        color = '#c34242' if gross['margin'] is not None and gross['margin'] < 0 else GREEN
        if gross['margin'] is None:
            color = MUTED
        self._text(c, edges[6]-10, y+16, format_number(gross['margin'])+' 원' if gross['margin'] is not None else '—',
                   color=color, size=13, bold=True, anchor='ne', tags=margin_tags)
        rate = '—' if gross['margin_rate'] is None else f'{gross["margin_rate"]:.1f}%'
        self._text(c, edges[6]-10, y+48, rate, color=color, size=11, anchor='ne', tags=margin_tags)
        if gross['margin'] is None:
            self._text(c, edges[5]+10, y+75, self._fit('·'.join(gross['missing'])+' 미확인',
                       edges[6]-edges[5]-20, size=8), color=MUTED, size=8, tags=margin_tags)
        self._text(c, edges[6]-10, y+104, '마진 상세 ›', color=BLUE, size=9, anchor='ne', tags=margin_tags)
        state = r['state']
        color = GREEN if state == '양호' else ORANGE if state in ('품절 임박', '품절') else MUTED
        middle = (edges[6]+edges[7])/2
        self._text(c, middle, y+32, state, color=color, bold=True, anchor='center')
        tags = ('action:pending', f'row:{index}')
        c.create_rectangle(edges[6]+8, y+65, edges[7]-8, y+99, fill='#f1f4f8', outline='', tags=tags)
        self._text(c, middle, y+82, '입고중 관리', size=9, anchor='center', tags=tags)

    def _action_at(self, event):
        x, y = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        index = int(y//self.ROW_HEIGHT)
        if not 0 <= index < len(self.visible_rows):
            return None, None
        if 0 <= x < 34:
            return 'check', index
        for item in reversed(self.canvas.find_overlapping(x, y, x, y)):
            tags = self.canvas.gettags(item)
            action = next((t.split(':', 1)[1] for t in tags if t.startswith('action:')), None)
            if action:
                return action, index
        return None, index

    def _click(self, event):
        self.canvas.focus_set()
        action, index = self._action_at(event)
        if index is None:
            return
        self._focus_index = index
        row = self.visible_rows[index]
        if action == 'check':
            self.toggle_selection(row['key'], row['key'] not in self.selected)
        elif action == 'product':
            self.open_product(row)
        elif action == 'pending':
            self.pending_dialog([row])
        elif action == 'margin':
            self.margin_detail_dialog(row)

    def _motion(self, event):
        action, _ = self._action_at(event)
        cursor = 'hand2' if action else ''
        if self.canvas.cget('cursor') != cursor:
            self.canvas.configure(cursor=cursor)

    def _header_click(self, event):
        if self.header.canvasx(event.x) < 34:
            self.all_checked.set(not self.all_checked.get())
            self.select_page()

    def _keyboard(self, direction):
        if self.visible_rows:
            self._focus_index = max(0, min(len(self.visible_rows)-1, self._focus_index+direction))
            y = self._focus_index*self.ROW_HEIGHT
            top = self.canvas.canvasy(0)
            height = self.canvas.winfo_height()
            if y < top or y+self.ROW_HEIGHT > top+height:
                target = y if y < top else y+self.ROW_HEIGHT-height+32
                self.canvas.yview_moveto(target/(len(self.visible_rows)*self.ROW_HEIGHT))
        return 'break'

    def _keyboard_select(self, event=None):
        if self.visible_rows:
            row = self.visible_rows[self._focus_index]
            self.toggle_selection(row['key'], row['key'] not in self.selected)
        return 'break'

    def _keyboard_edit(self, event=None):
        if self.visible_rows:
            self.pending_dialog([self.visible_rows[self._focus_index]])
        return 'break'

    def _keyboard_margin(self, event=None):
        if self.visible_rows:
            self.margin_detail_dialog(self.visible_rows[self._focus_index])
        return 'break'

    def go_page(self, direction):
        self.page_number += direction
        self.render()

    def toggle_selection(self, key, checked):
        if checked:
            self.selected.add(key)
        else:
            self.selected.discard(key)
        self._selection_changed()

    def select_page(self):
        checked = self.all_checked.get()
        for row in self.visible_rows:
            if checked:
                self.selected.add(row['key'])
            else:
                self.selected.discard(row['key'])
        self._selection_changed()

    def _selection_changed(self):
        self.all_checked.set(bool(self.visible_rows) and all(r['key'] in self.selected for r in self.visible_rows))
        self.selection_label.config(text=f'검색 결과 {len(self.filtered_rows):,}개  ·  선택 {len(self.selected):,}개')
        self._generation += 1
        self._draw_header()
        self._schedule_draw()

    def open_product(self, row):
        pid = str(row.get('exposed_product_id') or '').strip()
        oid = str(row.get('option_id') or '').strip()
        if pid and oid:
            webbrowser.open('https://www.coupang.com/vp/products/' + urllib.parse.quote(pid, safe='')
                            + '?vendorItemId=' + urllib.parse.quote(oid, safe=''))
        else:
            messagebox.showinfo('상품 링크', '상품 관리에서 노출상품 ID와 옵션 ID를 먼저 연결하세요.')

    def edit_selected(self):
        rows = [r for r in self.rows if r['key'] in self.selected]
        if not rows:
            messagebox.showinfo('입고중 관리', '왼쪽 체크박스로 상품을 선택하세요.')
            return
        self.pending_dialog(rows)

    def _dialog(self, title):
        win = tk.Toplevel(self)
        win.title(title)
        win.transient(self.winfo_toplevel())
        win.resizable(False, False)
        frame = ttk.Frame(win, padding=22)
        frame.pack(fill='both', expand=True)
        win.grab_set()
        return win, frame

    def margin_detail_dialog(self, row):
        existing = getattr(self, 'margin_window', None)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            return
        context = gross_margin.load_context(self.app.db)
        current = gross_margin.evaluate(row, context, allow_reference_price=False)
        win, frame = self._dialog('현재 판매가 마진 상세')
        self.margin_window = win
        win.result = current
        ttk.Label(frame, text='현재 판매가 마진 상세', font=(self.font, 15, 'bold')).grid(
            row=0, column=0, columnspan=2, sticky='w')
        title = (current['model'] or '모델 미연결')+' · '+(current['option_name'] or current['product_name'])
        ttk.Label(frame, text=title, wraplength=480).grid(row=1, column=0, columnspan=2, sticky='w', pady=(8, 10))
        ttk.Label(frame, text='상품 관리에 저장된 현재 판매가로 자동 계산합니다.',
                  foreground='#526a85').grid(row=2, column=0, columnspan=2, sticky='w', pady=(0, 12))
        ttk.Label(frame, text='항목', width=24).grid(row=3, column=0, sticky='w', pady=(0, 6))
        ttk.Label(frame, text='현재 판매가 기준', width=20, anchor='e').grid(row=3, column=1, padx=(20, 0))
        fields = [('sale', '현재 판매가'), ('cost', '원가'), ('commission_fee', '판매 수수료 (VAT 포함)'),
                  ('inout_vat', '입출고비 (VAT 포함)'), ('shipping_vat', '배송비 (VAT 포함)'),
                  ('margin', '개당 예상 마진'), ('margin_rate', '예상 마진율')]
        labels = {}
        for index, (key, title) in enumerate(fields, 4):
            value = current[key[:-4]] if key in ('inout_vat', 'shipping_vat') else current[key]
            if key in ('inout_vat', 'shipping_vat') and value is not None:
                value *= 1.1
            text = '—' if value is None else f'{value:.1f}%' if key == 'margin_rate' else f'{value:,.0f} 원'
            bold = key in ('sale', 'margin', 'margin_rate')
            color = MUTED if value is None else (
                ('#c34242' if value < 0 else GREEN) if key in ('margin', 'margin_rate') else INK)
            font = (self.font, 12 if bold else 10, 'bold' if bold else 'normal')
            ttk.Label(frame, text=title, font=font).grid(row=index, column=0, sticky='w', pady=5)
            label = ttk.Label(frame, text=text, foreground=color, font=font)
            label.grid(row=index, column=1, sticky='e')
            labels[key] = label
        win.result_labels = labels
        missing = current['missing']
        message = ('계산 미확인: '+', '.join(missing)+' · 그로스 마진에서 원가·사이즈를 확인하세요.') if missing else ''
        status = ttk.Label(frame, text=message, wraplength=480, foreground='#b35f1f')
        status.grid(row=11, column=0, columnspan=2, sticky='w', pady=(10, 6))
        win.result_status = status
        source = f'원가: {current["cost_source"]} · 사이즈: {current["size"] or "미확인"} · 수수료율: {current["commission"]:g}% (VAT 별도)'
        ttk.Label(frame, text=source, wraplength=550, foreground=MUTED).grid(row=12, column=0, columnspan=2, sticky='w', pady=3)
        stored = f'쿠폰가 {format_number(row.get("coupon_price"))}원 · 지도가 {format_number(row.get("map_price"))}원'
        ttk.Label(frame, text=stored+' (저장값)', foreground=MUTED).grid(row=13, column=0, columnspan=2, sticky='w', pady=3)
        ttk.Label(frame, text='현재 판매가의 요금 구간 적용 · 저장 요금표 기준 · 보관·반품비 제외',
                  foreground=MUTED).grid(row=14, column=0, columnspan=2, sticky='w', pady=(3, 12))
        def close(event=None):
            win.destroy()
            self.canvas.focus_set()
        ttk.Button(frame, text='닫기', command=close).grid(row=15, column=1, sticky='e')
        win.bind('<Escape>', close)
        win.protocol('WM_DELETE_WINDOW', close)
        win.update_idletasks()
        parent = self.winfo_toplevel()
        x = parent.winfo_rootx()+max(0, (parent.winfo_width()-win.winfo_width())//2)
        y = parent.winfo_rooty()+max(0, (parent.winfo_height()-win.winfo_height())//2)
        x = max(0, min(x, win.winfo_screenwidth()-win.winfo_width()))
        y = max(0, min(y, win.winfo_screenheight()-win.winfo_height()-40))
        win.geometry(f'+{x}+{y}')
        win.focus_set()

    def pending_dialog(self, rows):
        if any(not r.get('option_id') for r in rows):
            messagebox.showinfo('입고중 관리', '옵션 ID가 없는 상품은 상품정보를 먼저 연결하세요.')
            return
        win, frame = self._dialog('입고중 수량 관리')
        ttk.Label(frame, text=f'{len(rows)}개 옵션의 입고중 수량', font=(self.font, 13, 'bold')).grid(row=0, column=0, columnspan=2, sticky='w')
        ttk.Label(frame, text='이미 보냈지만 판매가능 재고에 아직 반영되지 않은 수량입니다.\n완료된 물량은 제외하고, 남은 입고중 수량을 입력하세요.').grid(row=1, column=0, columnspan=2, sticky='w', pady=(10, 16))
        values = [rows[0]['pending'], rows[0]['pack'], rows[0]['pending_note']] if len(rows) == 1 else ['', 1, '']
        variables = [tk.StringVar(value='' if value is None else str(value)) for value in values]
        for index, (label, var) in enumerate(zip(['입고중 수량 (개)', '입고 단위 (개)', '메모'], variables), 2):
            ttk.Label(frame, text=label).grid(row=index, column=0, sticky='w', pady=7)
            ttk.Entry(frame, textvariable=var, width=33).grid(row=index, column=1, pady=7, padx=(18, 0))
        if len(rows) > 1:
            ttk.Label(frame, text='선택한 각 옵션에 같은 수량·입고 단위·메모를 적용합니다.', foreground='#a65c12').grid(row=5, column=0, columnspan=2, sticky='w', pady=8)
        def save():
            try:
                vendor = self.app.db.setting('api_vendor_id', '').strip()
                save_pending(self.app.db.conn, vendor, [r['option_id'] for r in rows],
                             variables[0].get(), variables[1].get(), variables[2].get())
            except (ValueError, sqlite3.Error) as exc:
                messagebox.showerror('입고중 관리', str(exc), parent=win)
                return
            win.destroy()
            self.refresh()
        ttk.Button(frame, text='저장', command=save).grid(row=6, column=1, sticky='e', pady=(15, 0))
        ttk.Button(frame, text='취소', command=win.destroy).grid(row=6, column=0, sticky='w', pady=(15, 0))

    def settings_dialog(self):
        win, frame = self._dialog('권장 입고량 계산 기준')
        fields = [('rg_lead_days', '입고 소요일', '5'), ('rg_target_days', '확보할 판매일수', '14'),
                  ('rg_safety_days', '여유일수', '3')]
        variables = {}
        ttk.Label(frame, text='권장 입고량 계산 기준', font=(self.font, 13, 'bold')).grid(row=0, column=0, columnspan=2, sticky='w', pady=(0, 14))
        for index, (key, label, default) in enumerate(fields, 1):
            var = tk.StringVar(value=self.app.db.setting(key, default))
            variables[key] = var
            ttk.Label(frame, text=label + ' (일)').grid(row=index, column=0, sticky='w', pady=7)
            ttk.Spinbox(frame, textvariable=var, from_=0, to=365, width=12).grid(row=index, column=1, padx=20, pady=7)
        ttk.Label(frame, text='하루 평균 = 최근 30일 판매량 ÷ 30\n권장량 = 하루 평균 × (소요일 + 확보일수 + 여유일수)\n             − 판매가능 재고 − 입고중 수량\n\n입고 단위로 올림하며, 음수는 0개로 표시합니다.\n입고중 미입력·판매량 0·24시간 지난 조회는 추천을 보류합니다.\n신상품·품절 기간·행사에 따른 판매 변화는 직접 확인하세요.', justify='left').grid(row=4, column=0, columnspan=2, pady=15, sticky='w')
        def save():
            try:
                values = {key: whole_number(var.get(), '일수', nullable=False) for key, var in variables.items()}
                if any(v > 365 for v in values.values()) or values['rg_target_days'] < 1:
                    raise ValueError('확보할 판매일수는 1~365일, 나머지는 0~365일로 입력하세요.')
                with self.app.db.conn:
                    for key, value in values.items():
                        self.app.db.conn.execute('INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)', (key, str(value)))
            except (ValueError, sqlite3.Error) as exc:
                messagebox.showerror('계산 기준', str(exc), parent=win)
                return
            win.destroy()
            self.refresh()
        ttk.Button(frame, text='저장', command=save).grid(row=5, column=1, sticky='e')

    def show_help(self):
        messagebox.showinfo('재고 항목 안내',
            '• 최근 성공한 재고 API 응답에 포함된 옵션만 표시합니다.\n'
            '  상품 등록정보의 로켓그로스 분류만으로 추가하지 않습니다.\n'
            '  재고·최근 30일 판매가 모두 0이고 입력된 입고중 물량도 없으면 제외합니다.\n'
            '  최근 판매가 있는 품절 상품과 미확인 수량은 그대로 표시합니다.\n'
            '• 판매가능 / 지난 30일: 로켓그로스 재고 API 응답입니다.\n'
            '• 어제 / 지난 7일: 한국시간 기준, 옵션 ID가 연결된 저장 매출만 합산합니다.\n'
            '  기록이 없는 날은 0으로 추정하지 않으며, 기록된 일수를 함께 표시합니다.\n'
            '• 입고중: 직접 입력합니다. 입고가 끝나면 남은 수량으로 수정하세요.\n'
            '• 입고권장 / 소진 예상 / 상품 상태: 최근 30일 판매속도로 자체 계산합니다.\n'
            '• 판매가: 상품 관리에 저장된 값입니다. 쿠폰·지도가 등은 마진 상세에서 확인합니다.\n'
            '• 개당 예상 마진: 그로스 마진과 같은 원가·사이즈·요금표·수수료율로 계산합니다.\n'
            '  판매가 − 수수료(VAT) − 원가 − 물류비(VAT). 보관비·반품비 등은 제외합니다.\n'
            '  원가·사이즈·요금 등이 없으면 0원 대신 —와 미확인 사유를 표시합니다.\n'
            '• 판매가·마진 클릭 또는 Ctrl+Enter: 현재 판매가의 원가·수수료·물류비 내역을 확인합니다.\n'
            '  실제 판매가·상품 DB는 변경하지 않으며 쿠팡 API 요청도 하지 않습니다.\n'
            '• 예상 비용: 저장 요금표의 수수료와 입출고·배송비 합계입니다.\n'
            '  원가·보관비·반품비는 포함하지 않습니다. WING 청구 금액과 다를 수 있습니다.\n'
            '• 상품 이미지: 상품 API 동기화로 받은 대표이미지를 표시합니다.\n'
            '• 24시간 지난 재고는 재조회가 필요하며, 최신 응답에서 빠진 옵션은 목록에서 제외합니다.\n'
            '• 입고권장은 쿠팡이 제공한 추천 수량이 아닙니다.')

    def export_csv(self):
        rows = [r for r in self.rows if r['key'] in self.selected] if self.selected else self.filtered_rows
        if not rows:
            messagebox.showinfo('CSV 저장', '저장할 상품이 없습니다.')
            return
        path = filedialog.asksaveasfilename(defaultextension='.csv', initialfile=f'로켓그로스_재고_{now_kst():%Y%m%d}.csv',
                                          filetypes=[('CSV 파일', '*.csv')])
        if not path:
            return
        columns = [('model', '모델명'), ('product_name', '상품명'), ('option_name', '옵션명'), ('option_id', '옵션ID'),
                   ('stock', '주문가능재고'), ('pending', '입고중_직접입력'), ('recommended', '권장추가입고_자체계산'),
                   ('pack', '입고단위'), ('sales_30', '최근30일판매량_API'), ('yesterday', '어제판매량_저장자료'),
                   ('last7', '7일판매량_저장자료'), ('recorded_days', '7일중기록일수'), ('days', '소진예상일수'),
                   ('sale_price', '판매가_저장값'), ('coupon_price', '쿠폰가_저장값'), ('estimated_cost', '예상개당비용_자체계산'),
                   ('unit_cost', '원가'), ('logistics', '물류비_VAT포함'),
                   ('margin', '개당예상마진'), ('margin_rate', '예상마진율_퍼센트'), ('margin_missing', '마진미확인사유'),
                   ('state', '재고상태_자체판정'), ('checked_at', '재고조회시각'), ('pending_updated', '입고중수정시각'),
                   ('pending_note', '입고중메모')]
        def cell(value):
            if value is None:
                return ''
            if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
                return "'" + value
            if isinstance(value, float):
                return round(value, 2)
            return value
        try:
            with open(path, 'w', encoding='utf-8-sig', newline='') as out:
                writer = csv.writer(out)
                writer.writerow([title for _, title in columns])
                writer.writerows([[cell(r.get(key)) for key, _ in columns] for r in rows])
        except OSError as exc:
            messagebox.showerror('CSV 저장', str(exc))
            return
        messagebox.showinfo('CSV 저장', f'{len(rows):,}개 옵션을 저장했습니다.')

    def _poll_images(self):
        changed = self.thumbnails.drain()
        visible_urls = {str(r.get('image_url') or '') for r in self._drawn_rows.values()}
        if changed & visible_urls:
            self._generation += 1
            self._schedule_draw()
        # Retry requests skipped when the bounded queue was full.
        for url in visible_urls:
            self.thumbnails.get(url)
        self.after_job = self.after(150, self._poll_images)
