"""Independent daily comparison window; reads saved API orders only."""
from datetime import timedelta
import logging
import sqlite3
import tkinter as tk
from tkinter import ttk, messagebox

import daily_comparison
import rocket_sales
import window_tools


BLUE = '#1762b5'
RED = '#c13d3d'
MUTED = '#667085'


def number(value, suffix=''):
    if value is None:
        return '—'
    return f'{value:,.0f}{suffix}'


def change(delta, percent=None, suffix=''):
    if delta is None:
        return '비교 자료가 필요합니다'
    text = f'{delta:+,.0f}{suffix}' if delta else f'변동 없음{suffix}'
    if percent is not None:
        text += f' ({percent:+.2f}%)'
    elif delta:
        text += ' (비교일 0 · 증감률 계산 불가)'
    return text


class ComparisonWindow(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title('일일 비교 · 로켓그로스 API')
        self.geometry('1440x850')
        self.minsize(1100, 720)
        self.report = None
        self._job = None
        self._revision = None
        today = rocket_sales.today_kst()
        self.base_date = tk.StringVar(value=(today-timedelta(days=1)).isoformat())
        self.compare_date = tk.StringVar(value=(today-timedelta(days=2)).isoformat())
        self.query = tk.StringVar()
        frame = ttk.Frame(self, padding=18)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='일일 비교', font=('맑은 고딕', 20, 'bold')).pack(anchor='w')
        ttk.Label(frame, text='로켓그로스 출고 주문 · 한국시간 결제일 기준 · 취소/반품 차감 전 잠정치',
                  foreground='#946200').pack(anchor='w', pady=(3, 10))
        bar = ttk.Frame(frame)
        bar.pack(fill='x')
        for label, variable in [('비교일', self.compare_date), ('기준일', self.base_date)]:
            ttk.Label(bar, text=label).pack(side='left', padx=(0, 6))
            entry = ttk.Entry(bar, textvariable=variable, width=13)
            entry.pack(side='left', padx=(0, 12))
            entry.bind('<Return>', lambda event: self.refresh())
        ttk.Button(bar, text='비교 보기', command=self.refresh).pack(side='left')
        ttk.Button(bar, text='전일과 비교', command=self.previous_day).pack(side='left', padx=6)
        self.collect_button = ttk.Button(bar, text='최근 30일 이어받기', command=app.sync_recent_daily_sales)
        self.collect_button.pack(side='right')
        self.status = ttk.Label(frame, text='', foreground=MUTED, wraplength=1320)
        self.status.pack(fill='x', pady=(8, 8))

        cards = ttk.Frame(frame)
        cards.pack(fill='x')
        self.cards = {}
        for i, (key, title) in enumerate([('revenue', 'API 매출 · 잠정'), ('quantity', 'API 판매량 · 잠정')]):
            card = ttk.LabelFrame(cards, text=title, padding=(14, 8))
            card.grid(row=0, column=i, sticky='nsew', padx=(0, 10) if i == 0 else 0)
            cards.columnconfigure(i, weight=1)
            value = ttk.Label(card, text='—', font=('맑은 고딕', 23, 'bold'))
            value.pack(side='left')
            detail = ttk.Label(card, text='', justify='right')
            detail.pack(side='right')
            self.cards[key] = (value, detail)

        tabs = ttk.Notebook(frame)
        self.tabs = tabs
        tabs.pack(fill='both', expand=True, pady=(12, 0))
        summary = ttk.Frame(tabs, padding=10)
        all_options = ttk.Frame(tabs, padding=10)
        self.all_options = all_options
        tabs.add(summary, text='실적 요약')
        tabs.add(all_options, text='옵션별 전체 비교')
        summary.columnconfigure(0, weight=1)
        summary.columnconfigure(1, weight=1)
        summary.rowconfigure(1, weight=1)
        overview = ttk.LabelFrame(summary, text='전체 실적 비교', padding=10)
        overview.grid(row=0, column=0, sticky='nsew', padx=(0, 8), pady=(0, 8))
        self.overview = self.make_table(overview, [('metric', '항목', 100), ('previous', '비교일', 130),
            ('base', '기준일', 130), ('delta', '증감', 170)], height=2)
        self.insight = ttk.Label(overview, text='', wraplength=590, foreground=MUTED)
        self.insight.pack(fill='x', pady=(8, 0))
        graph = ttk.LabelFrame(summary, text='최근 5일 API 매출 · 기준일까지', padding=5)
        graph.grid(row=0, column=1, sticky='nsew', pady=(0, 8))
        self.chart = tk.Canvas(graph, height=165, background='white', highlightthickness=0)
        self.chart.pack(fill='both', expand=True)
        self.chart.bind('<Configure>', lambda event: self.draw_chart())
        self.ranks = {}
        for i, (key, title) in enumerate([('increases', '판매량 증가 TOP 5'), ('decreases', '판매량 감소 TOP 5')]):
            box = ttk.LabelFrame(summary, text=title, padding=8)
            box.grid(row=1, column=i, sticky='nsew', padx=(0, 8) if i == 0 else 0)
            self.ranks[key] = self.make_table(box, [('product', '상품명', 210), ('option', '옵션', 125), ('option_id', '옵션 ID', 110),
                ('previous', '비교일', 55), ('base', '기준일', 55), ('delta', '증감', 55)], height=5)
        self.all_info = ttk.Label(all_options, text='', foreground=MUTED)
        self.all_info.pack(anchor='w', pady=(0, 8))
        self.all_table = self.make_table(all_options, [('product', '상품명', 280), ('option', '옵션', 160), ('option_id', '옵션 ID', 125),
            ('previous', '비교일 판매량', 100), ('base', '기준일 판매량', 100), ('delta', '판매량 증감', 100),
            ('previous_revenue', '비교일 매출', 130), ('revenue', '기준일 매출', 130), ('revenue_delta', '매출 증감', 130)], height=12)
        ttk.Label(frame, text='저장된 API 자료로 자동 계산합니다. 취소 금액·수량은 제공하지 않습니다. '
                  '수집 완료도 확정 매출을 뜻하지 않습니다.  |  F5 옵션 검색 · Esc 검색 해제',
                  foreground=MUTED).pack(anchor='w', pady=(8, 0))
        self.bind('<F5>', self.search_dialog)
        self.bind('<Escape>', self.clear_search)
        self.bind('<Destroy>', self._destroyed, add='+')
        self.refresh()
        self.after_idle(lambda: window_tools.place(self, app))
        self._job = self.after(2000, self._poll)

    @staticmethod
    def make_table(parent, columns, height):
        box = ttk.Frame(parent)
        box.pack(fill='both', expand=True)
        tree = ttk.Treeview(box, columns=[c[0] for c in columns], show='headings', height=height)
        for i, (key, title, width) in enumerate(columns):
            tree.heading(key, text=title)
            tree.column(key, width=width, minwidth=55, stretch=(i == 0), anchor='w' if i < 2 else 'e')
        vertical = ttk.Scrollbar(box, orient='vertical', command=tree.yview)
        horizontal = ttk.Scrollbar(box, orient='horizontal', command=tree.xview)
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)
        tree.grid(row=0, column=0, sticky='nsew')
        vertical.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        tree.tag_configure('up', foreground=BLUE)
        tree.tag_configure('down', foreground=RED)
        return tree

    def previous_day(self):
        try:
            from datetime import date
            day = date.fromisoformat(self.base_date.get().strip())
            self.compare_date.set((day-timedelta(days=1)).isoformat())
        except (ValueError, OverflowError):
            pass
        self.refresh()

    def _current_revision(self):
        conn = self.app.db.conn
        return conn.total_changes, conn.execute('PRAGMA data_version').fetchone()[0]

    def refresh(self, quiet=False):
        try:
            revision = self._current_revision()
            vendor = self.app.db.setting('api_vendor_id', '').strip()
            allowed = None
            limited = self.app.multitap_only_enabled()
            if limited:
                allowed = {str(r['option_id']) for r in self.app.db.query(
                    "SELECT p.option_id FROM products p WHERE p.fulfillment_type='로켓그로스' AND "
                    + self.app._multitap_product_clause('p'))}
            report = daily_comparison.build_report(self.app.db.conn, vendor,
                self.base_date.get().strip(), self.compare_date.get().strip(), option_ids=allowed)
        except (ValueError, sqlite3.Error) as exc:
            if isinstance(exc, sqlite3.Error):
                logging.getLogger('CoupangManager').exception('일일 비교 읽기 실패')
            if not quiet:
                messagebox.showwarning('일일 비교', str(exc), parent=self)
            self.status.configure(text=str(exc), foreground=RED)
            self.report = None
            for value, detail in self.cards.values():
                value.configure(text='—')
                detail.configure(text='날짜와 저장 자료를 확인하세요', foreground=MUTED)
            for tree in [self.overview, self.all_table, *self.ranks.values()]:
                tree.delete(*tree.get_children())
            self.chart.delete('all')
            self.insight.configure(text='유효한 날짜를 선택해 다시 비교하세요.')
            self.all_info.configure(text='비교 자료를 표시할 수 없습니다.')
            return False
        self.report = report
        # Retain the revision from BEFORE reading: a collection commit between
        # the report snapshot and this assignment must trigger the next poll.
        self._revision = revision
        state_names = {'complete': '수집 완료 · 잠정', 'missing': '미수집', 'unverified': '재확인 필요'}
        labels = []
        for day in (report['previous'], report['base']):
            stamp = (day['fetched_at'] or '').replace('T', ' ')[:16]
            labels.append(f"{day['date']} {state_names[day['state']]}" + (f' (저장 {stamp})' if stamp else ''))
        scope = '멀티탭 관련 등록 옵션만' if limited else '로켓그로스 전체 옵션'
        self.status.configure(text='  |  '.join(labels) + '  |  ' + scope,
                              foreground=MUTED if report['comparable'] else '#946200')
        for key, suffix in [('revenue', '원'), ('quantity', '개')]:
            value, detail = self.cards[key]
            value.configure(text=number(report['base'][key], suffix))
            delta = report[key+'_delta']
            detail.configure(text='비교일 '+number(report['previous'][key], suffix)+'\n'+change(delta, report[key+'_percent'], suffix),
                             foreground=BLUE if delta and delta > 0 else RED if delta and delta < 0 else MUTED)
        self.overview.delete(*self.overview.get_children())
        for key, label, suffix in [('revenue', '매출 · 잠정', '원'), ('quantity', '판매량 · 잠정', '개')]:
            self.overview.insert('', 'end', values=(label, number(report['previous'][key], suffix),
                number(report['base'][key], suffix), change(report[key+'_delta'], report[key+'_percent'], suffix)))
        counts = report['counts']
        self.insight.configure(text=(f"판매량 증가 {counts['increased']}개 · 감소 {counts['decreased']}개 · 동일 {counts['unchanged']}개 옵션\n"
            '두 날짜 중 주문이 있는 옵션을 비교합니다. 감소 원인은 별도 확인이 필요합니다.'
            if report['comparable'] else '두 날짜 모두 수집이 끝나야 증감과 순위를 계산합니다.\n미수집·재확인 날짜를 0으로 처리하지 않습니다.'))
        for key, tree in self.ranks.items():
            tree.delete(*tree.get_children())
            for row in report[key]:
                tree.insert('', 'end', values=self.row_values(row)[:6], tags=('up' if row['quantity_delta'] > 0 else 'down',))
            if not report[key]:
                tree.insert('', 'end', values=('해당 옵션 없음' if report['comparable'] else '두 날짜의 수집 완료가 필요합니다', '', '', '', '', ''))
        self.render_all()
        self.draw_chart()
        return True

    @staticmethod
    def row_values(row):
        name = row['product_name'] or '상품명 확인 필요'
        return (name, row['option_name'], row['option_id'], number(row['previous_quantity']), number(row['quantity']),
                f"{row['quantity_delta']:+,}", number(row['previous_revenue']), number(row['revenue']),
                f"{row['revenue_delta']:+,.0f}")

    def render_all(self):
        if self.report is None:
            return
        term = self.query.get().strip().casefold()
        rows = sorted((r for r in self.report['rows'] if not term or term in
                ' '.join(str(r[k]) for k in ('option_id', 'product_name', 'option_name')).casefold()),
                key=lambda r: (-abs(r['quantity_delta']), r['option_id']))
        self.all_table.delete(*self.all_table.get_children())
        for row in rows:
            delta = row['quantity_delta']
            self.all_table.insert('', 'end', values=self.row_values(row), tags=('up' if delta > 0 else 'down' if delta < 0 else '',))
        self.all_info.configure(text=f"옵션 {len(rows)}개 · 판매량 증감 큰 순 · F5 검색 / Esc 해제"
            + (f' · 검색: {self.query.get()}' if term else '')
            + (' · 비교 불가: 두 날짜 모두 수집이 필요합니다' if not self.report['comparable'] else ''))

    def search_dialog(self, event=None):
        dialog = tk.Toplevel(self)
        dialog.title('비교 옵션 검색')
        dialog.geometry('480x130')
        box = ttk.Frame(dialog, padding=14)
        box.pack(fill='both', expand=True)
        ttk.Label(box, text='상품명 / 옵션명 / 옵션 ID · 전체 비교 목록에 적용').pack(anchor='w')
        text = tk.StringVar(value=self.query.get())
        entry = ttk.Entry(box, textvariable=text)
        entry.pack(fill='x', pady=8)
        def finish(event=None):
            if window_tools.exists(self):
                self.query.set(text.get())
                self.render_all()
                self.tabs.select(self.all_options)
            dialog.destroy()
            return 'break'
        ttk.Button(box, text='검색', command=finish).pack(side='right')
        entry.bind('<Return>', finish)
        dialog.bind('<Escape>', lambda event: dialog.destroy())
        window_tools.place(dialog, self)
        entry.focus_set()
        return 'break'

    def clear_search(self, event=None):
        self.query.set('')
        self.render_all()
        return 'break'

    def draw_chart(self):
        if self.report is None:
            return
        canvas = self.chart
        canvas.delete('all')
        width, height = max(canvas.winfo_width(), 300), max(canvas.winfo_height(), 150)
        days = self.report['trend']
        peak = max((d['revenue'] or 0 for d in days), default=0) or 1
        floor = height-38
        step = (width-24)/5
        canvas.create_line(12, floor, width-12, floor, fill='#d0d5dd')
        for i, day in enumerate(days):
            center = 12+step*(i+.5)
            amount = day['revenue']
            label = day['date'][5:].replace('-', '/')
            canvas.create_text(center, floor+16, text=label, fill=MUTED, font=('맑은 고딕', 9))
            if amount is None:
                canvas.create_text(center, floor-20, text='재확인 필요' if day['state'] == 'unverified' else '미수집',
                                   fill='#946200', font=('맑은 고딕', 9))
                continue
            bar_height = float(amount/peak)*(height-72)
            canvas.create_rectangle(center-step*.25, floor-max(2, bar_height), center+step*.25,
                                    floor, fill=BLUE if i == 4 else '#8ebae8', outline='')
            canvas.create_text(center, floor-bar_height-13, text=number(amount, '원'),
                               fill='#344054', font=('맑은 고딕', 9))

    def _poll(self):
        self._job = None
        if not window_tools.exists(self) or not window_tools.exists(self.app):
            return
        try:
            self.collect_button.configure(state='disabled' if self.app.api_busy else 'normal')
            if self._current_revision() != self._revision:
                self.refresh(quiet=True)
        except sqlite3.Error:
            logging.getLogger('CoupangManager').exception('일일 비교 갱신 대기')
            self.status.configure(text='저장 자료를 읽지 못했습니다. 잠시 후 다시 확인합니다.', foreground=RED)
        finally:
            self._job = self.after(2000, self._poll)

    def _destroyed(self, event):
        if event.widget is self and self._job is not None:
            self.after_cancel(self._job)
            self._job = None
