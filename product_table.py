"""Compact product groups, with canvas drawing limited to the visible rows.

The small Treeview-compatible selection interface preserves the application's
copy, edit, history and context-menu actions. This widget never requests data.
"""
import bisect
import os
import tkinter as tk
from tkinter import font as tkfont, ttk


class ProductTable(tk.Canvas):
    COLUMNS = ('fulfillment', 'sale_status', 'sale', 'map', 'stock', 'details')
    HEADINGS = ('옵션 / 모델', '배송구분', '판매상태', '판매가 (원)', '지도가 (원)', '재고', '상세정보')
    INK, MUTED, LINE = '#202b3d', '#66758a', '#e2e8f0'
    BLUE = '#1763c6'

    def __init__(self, parent, app):
        self.app = app
        self.holder = ttk.Frame(parent)
        self.holder.pack(fill='both', expand=True)
        self.header = tk.Canvas(self.holder, height=40, bg='#f2f5f9', highlightthickness=0)
        super().__init__(self.holder, bg='white', highlightthickness=1,
                         highlightbackground=self.LINE, takefocus=True,
                         yscrollincrement=28)
        self.vbar = ttk.Scrollbar(self.holder, orient='vertical', command=self.yview)
        self.hbar = ttk.Scrollbar(self.holder, orient='horizontal', command=self.xview)
        self.header.grid(row=0, column=0, sticky='ew')
        self.grid(row=1, column=0, sticky='nsew')
        self.vbar.grid(row=1, column=1, sticky='ns')
        self.hbar.grid(row=2, column=0, sticky='ew')
        self.holder.columnconfigure(0, weight=1)
        self.holder.rowconfigure(1, weight=1)
        self.configure(yscrollcommand=self._yscroll, xscrollcommand=self._xscroll)
        family = '맑은 고딕' if os.name == 'nt' else 'Noto Sans CJK KR'
        self.regular = tkfont.Font(self, family=family, size=10)
        self.bold = tkfont.Font(self, family=family, size=10, weight='bold')
        self.small = tkfont.Font(self, family=family, size=9)
        self.row_height = max(58, self.regular.metrics('linespace') + self.small.metrics('linespace') + 17)
        self.group_height = self.row_height
        self.widths = [290, 112, 102, 108, 103, 90, 100]
        for n, heading in enumerate(self.HEADINGS):
            self.widths[n] = max(self.widths[n], self.small.measure(heading) + 30)
        self._width = sum(self.widths)
        self._nodes, self._groups, self._closed = {}, [], set()
        self._flat, self._tops, self._positions = [], [], {}
        self._selected, self._focus_iid, self._anchor = set(), '', ''
        self._active_cell, self._cell_marker, self._hover = None, None, ''
        self._total, self._revision, self._draw_job = 0, 0, None
        self._signature, self._fit_cache = None, {}
        self._drawn_rows = []
        self.bind('<Configure>', self._resize)
        self.bind('<Button-1>', self._click)
        self.bind('<Motion>', self._motion)
        self.bind('<Leave>', self._leave)
        self.bind('<MouseWheel>', self._wheel)
        self.bind('<Button-4>', lambda e: self._scroll_units(-3))
        self.bind('<Button-5>', lambda e: self._scroll_units(3))
        self.bind('<Up>', lambda e: self._move(-1, e))
        self.bind('<Down>', lambda e: self._move(1, e))
        self.bind('<Home>', lambda e: self._move(-len(self._flat), e))
        self.bind('<End>', lambda e: self._move(len(self._flat), e))
        self.bind('<Prior>', lambda e: self._move(-max(1, self.winfo_height()//self.row_height), e))
        self.bind('<Next>', lambda e: self._move(max(1, self.winfo_height()//self.row_height), e))
        self.bind('<Left>', lambda e: self._open_focused(False))
        self.bind('<Right>', lambda e: self._open_focused(True))
        self.bind('<Return>', self._activate)
        self.bind('<space>', self._activate)
        self.bind('<Control-a>', self._select_all)
        self.bind('<Control-c>', lambda e: app._tree_copy_current(self))
        self.bind('<Control-C>', lambda e: app._tree_copy_current(self))

    def destroy(self):
        if self._draw_job:
            self.after_cancel(self._draw_job)
            self._draw_job = None
        super().destroy()

    def __getitem__(self, key):
        return self.cget(key)

    def cget(self, key):
        if key == 'columns':
            return self.COLUMNS
        if key == 'show':
            return 'tree headings'
        return super().cget(key)

    def heading(self, column, option='text'):
        idx = 0 if column == '#0' else self.COLUMNS.index(column) + 1
        return self.HEADINGS[idx] if option == 'text' else {'text': self.HEADINGS[idx]}

    def set_groups(self, groups):
        self._groups, self._nodes = [], {}
        for group in groups:
            key = group['iid']
            self._groups.append(key)
            self._nodes[key] = dict(group, kind='group', children=[r['iid'] for r in group['rows']], values=('',)*6)
            for row in group['rows']:
                self._nodes[row['iid']] = dict(row, kind='option', parent=key, children=[])
        self._selected.intersection_update(self._nodes)
        self._active_cell = None
        self._hover = ''
        self._rebuild()
        self.yview_moveto(0)
        self.event_generate('<<TreeviewSelect>>')

    def _rebuild(self):
        self._flat, self._tops, self._positions = [], [], {}
        top = 0
        for key in self._groups:
            nodes = [key] + ([] if key in self._closed else self._nodes[key]['children'])
            for iid in nodes:
                self._positions[iid] = len(self._flat)
                self._flat.append(iid)
                self._tops.append(top)
                top += self.group_height if self._nodes[iid]['kind'] == 'group' else self.row_height
        self._total = top
        self._selected.intersection_update(self._flat)
        if self._focus_iid not in self._positions:
            self._focus_iid = self._flat[0] if self._flat else ''
        self.configure(scrollregion=(0, 0, self._width, max(1, top)))
        self._revision += 1
        self._schedule()

    def get_children(self, item=''):
        return tuple(self._groups if not item else self._nodes.get(str(item), {}).get('children', []))

    def item(self, item, option=None, **kwargs):
        key = str(item)
        node = self._nodes[key]
        if 'open' in kwargs and node['kind'] == 'group':
            if kwargs['open']:
                self._closed.discard(key)
            else:
                self._closed.add(key)
            self._active_cell = None
            self._rebuild()
            self.event_generate('<<TreeviewSelect>>')
        text = node['text']
        if node['kind'] == 'option' and node['model']:
            text += ' / ' + node['model']
        data = {'text': text, 'values': node['values'], 'open': key not in self._closed,
                'tags': (node['kind'],)}
        return data.get(option) if option else data

    def set_all_open(self, opened):
        if opened:
            self._closed.difference_update(self._groups)
        else:
            self._closed.update(self._groups)
        self._active_cell = None
        self._rebuild()
        self.event_generate('<<TreeviewSelect>>')

    def selection(self):
        return tuple(iid for iid in self._flat if iid in self._selected)

    def selection_set(self, *items):
        if len(items) == 1 and isinstance(items[0], (tuple, list, set)):
            items = items[0]
        self._selected = {str(i) for i in items if str(i) in self._positions}
        self._revision += 1
        self._schedule()
        self.event_generate('<<TreeviewSelect>>')

    def focus(self, item=None):
        if item is not None:
            self._focus_iid = str(item)
        return self._focus_iid

    def _edges(self):
        widths = list(self.widths)
        widths[0] += max(0, self._width - sum(widths))
        edges = [0]
        for width in widths:
            edges.append(edges[-1] + width)
        return edges

    def identify_row(self, y):
        pos = self.canvasy(y)
        idx = bisect.bisect_right(self._tops, pos) - 1
        return self._flat[idx] if 0 <= idx < len(self._flat) and pos < self._total else ''

    def identify_column(self, x):
        pos = self.canvasx(x)
        idx = bisect.bisect_right(self._edges(), pos) - 1
        return f'#{idx}' if 0 <= idx <= len(self.COLUMNS) else ''

    def identify_region(self, x, y):
        return 'cell' if self.identify_row(y) else ''

    def bbox(self, item, column=None):
        idx = self._positions.get(str(item))
        if idx is None:
            return ()
        y = self._tops[idx] - self.canvasy(0)
        height = self.group_height if self._nodes[str(item)]['kind'] == 'group' else self.row_height
        if y + height < 0 or y > self.winfo_height():
            return ()
        edges = self._edges()
        col = int(column[1:]) if column and str(column).startswith('#') else 0
        right = edges[col+1] if column else self._width
        return (int(edges[col]-self.canvasx(0)), int(y), int(right-edges[col]), height)

    def see(self, item):
        idx = self._positions.get(str(item))
        if idx is None or not self._total:
            return
        y = self._tops[idx]
        top, bottom = self.canvasy(0), self.canvasy(self.winfo_height())
        height = self.group_height if self._nodes[str(item)]['kind'] == 'group' else self.row_height
        if y < top:
            self.yview_moveto(y/self._total)
        elif y+height > bottom:
            self.yview_moveto((y+height-self.winfo_height())/self._total)

    def mark_cell(self, row, column):
        self._active_cell = (str(row), str(column))
        self._schedule()

    def clear_cell_marker(self):
        self._active_cell = None
        self._schedule()

    def _resize(self, event):
        self._width = max(sum(self.widths), event.width-2)
        self.configure(scrollregion=(0, 0, self._width, max(1, self._total)))
        self.header.configure(scrollregion=(0, 0, self._width, 40))
        self._draw_header()
        self._schedule()

    def _yscroll(self, first, last):
        self.vbar.set(first, last)
        self._schedule()

    def _xscroll(self, first, last):
        self.hbar.set(first, last)
        self.header.xview_moveto(first)

    def _schedule(self):
        if self._draw_job is None:
            self._draw_job = self.after_idle(self._draw)

    def _fit(self, text, width, font):
        text = ' '.join(str(text or '').split())
        key = (text, int(width), str(font))
        if key in self._fit_cache:
            return self._fit_cache[key]
        result = text
        if font.measure(text) > width:
            lo, hi = 0, len(text)
            while lo < hi:
                mid = (lo+hi+1)//2
                if font.measure(text[:mid]+'…') <= width:
                    lo = mid
                else:
                    hi = mid-1
            result = text[:lo]+'…'
        if len(self._fit_cache) > 2048:
            self._fit_cache.clear()
        self._fit_cache[key] = result
        return result

    def _text(self, x, y, text, font=None, color=None, anchor='w', canvas=None):
        return (canvas if canvas is not None else self).create_text(
            x, y, text=text, font=font or self.regular,
            fill=color or self.INK, anchor=anchor)

    def _draw_header(self):
        self.header.delete('all')
        edges = self._edges()
        for col, label in enumerate(self.HEADINGS):
            numeric = col in (3, 4, 5)
            x = edges[col+1]-14 if numeric else edges[col]+14
            self._text(x, 20, label, self.small, self.MUTED, 'e' if numeric else 'w', self.header)
        self.header.create_line(0, 39, self._width, 39, fill=self.LINE)
        for col in (3, 6):
            self.header.create_line(edges[col], 0, edges[col], 40, fill=self.LINE)

    def _draw(self):
        self._draw_job = None
        top = max(0, self.canvasy(0))
        first = max(0, bisect.bisect_right(self._tops, top)-2)
        end = min(len(self._flat), bisect.bisect_right(self._tops, top+self.winfo_height())+1)
        signature = (first, end, self._width, self._revision, self._hover, self._active_cell)
        if signature == self._signature:
            return
        self._signature = signature
        super().delete('all')
        edges = self._edges()
        self._drawn_rows = self._flat[first:end]
        if not self._flat:
            self._text(24, 50, '조건에 맞는 상품이 없습니다. 검색어와 표시 설정을 확인하세요.', color=self.MUTED)
        for idx in range(first, end):
            iid, y = self._flat[idx], self._tops[idx]
            node = self._nodes[iid]
            group = node['kind'] == 'group'
            height = self.group_height if group else self.row_height
            bg = '#f4f7fb' if group else 'white'
            if iid in self._selected:
                bg = '#e9f2ff'
            elif iid == self._hover:
                bg = '#f7faff'
            self.create_rectangle(0, y, self._width, y+height, fill=bg, outline='')
            self.create_line(0, y+height-1, self._width, y+height-1, fill=self.LINE)
            if group:
                points = (13, y+15, 19, y+21, 13, y+27) if iid in self._closed else (11, y+18, 17, y+24, 23, y+18)
                self.create_line(*points, fill=self.MUTED, width=2)
                self._text(36, y+19, self._fit(node['text'], self._width-60, self.bold), self.bold)
                self._text(36, y+height-16, self._fit(node['meta'], self._width-60, self.small), self.small, self.MUTED)
                continue
            self._text(16, y+20, self._fit(node['text'], edges[1]-30, self.bold), self.bold)
            self._text(16, y+height-15, self._fit(node['model'] or '모델 미연결', edges[1]-30, self.small), self.small, self.MUTED)
            values = node['values']
            for col in (1, 3, 4, 5):
                numeric = col in (3, 4, 5)
                text = self._fit(values[col-1], edges[col+1]-edges[col]-24, self.regular)
                color = self.BLUE if col == 1 and values[0] == '로켓그로스' else self.INK
                self._text(edges[col+1]-14 if numeric else edges[col]+12, y+height/2,
                           text, color=color, anchor='e' if numeric else 'w')
            status = values[1]
            fg, fill = {'판매중':('#237146','#edf7f0'), '판매중지':('#b52f2f','#fff0ef'),
                        '품절':('#895710','#fff5dc')}.get(status, (self.MUTED, '#edf1f5'))
            bx, by = edges[2]+12, y+height/2-(9 if node['error'] else 0)
            label = self._fit(status, edges[3]-edges[2]-36, self.small)
            badge_width = self.small.measure(label)+14
            badge_height = self.small.metrics('linespace')+6
            self.create_rectangle(bx, by-badge_height/2, bx+badge_width, by+badge_height/2, fill=fill, outline='')
            self._text(bx+7, by, label, self.small, fg)
            if node['error']:
                self._text(bx, y+height-10, '조회 오류', self.small, '#895710')
            self._text(edges[6]+13, y+height/2-(8 if node['changed'] else 0), '상세 보기', self.small, self.BLUE)
            if node['changed']:
                self._text(edges[6]+13, y+height-12, '이력 변경', self.small, '#895710')
            for col in (3, 6):
                self.create_line(edges[col], y, edges[col], y+height, fill=self.LINE)
            if self._active_cell and self._active_cell[0] == iid:
                col = int(self._active_cell[1][1:])
                self.create_rectangle(edges[col]+1, y+1, edges[col+1]-1, y+height-2, outline=self.BLUE, width=2)

    def _click(self, event):
        self.focus_set()
        iid = self.identify_row(event.y)
        if not iid:
            return
        self._focus_iid = iid
        if event.state & 1 and self._anchor in self._positions:
            a, b = sorted((self._positions[self._anchor], self._positions[iid]))
            self.selection_set(self._flat[a:b+1])
        elif event.state & 4:
            chosen = set(self._selected)
            chosen.symmetric_difference_update([iid])
            self.selection_set(chosen)
            self._anchor = iid
        else:
            self.selection_set(iid)
            self._anchor = iid
        self.mark_cell(iid, self.identify_column(event.x))
        if self._nodes[iid]['kind'] == 'group' and self.canvasx(event.x) < 32:
            self.item(iid, open=iid in self._closed)
        return 'break'

    def _motion(self, event):
        iid = self.identify_row(event.y)
        if iid != self._hover:
            self._hover = iid
            self._schedule()
        action = iid and (self.identify_column(event.x) == '#6' or
                          (self._nodes[iid]['kind'] == 'group' and self.canvasx(event.x) < 32))
        self.configure(cursor='hand2' if action else '')

    def _leave(self, event):
        self._hover = ''
        self._schedule()

    def _scroll_units(self, count):
        self.yview_scroll(count, 'units')
        return 'break'

    def _wheel(self, event):
        if event.delta:
            self._scroll_units((-1 if event.delta > 0 else 1)*3)
        return 'break'

    def _move(self, distance, event):
        if not self._flat:
            return 'break'
        old = self._positions.get(self._focus_iid, -1 if distance > 0 else len(self._flat))
        idx = max(0, min(len(self._flat)-1, old+distance))
        iid = self._flat[idx]
        if event.state & 1 and self._anchor in self._positions:
            a, b = sorted((self._positions[self._anchor], idx))
            self.selection_set(self._flat[a:b+1])
        else:
            self.selection_set(iid)
            self._anchor = iid
        self._focus_iid = iid
        self._active_cell = None
        self.see(iid)
        return 'break'

    def _open_focused(self, opened):
        node = self._nodes.get(self._focus_iid)
        if node:
            key = self._focus_iid if node['kind'] == 'group' else node['parent']
            self.item(key, open=opened)
            self.focus(key)
            self.selection_set(key)
            self.see(key)
        return 'break'

    def _activate(self, event):
        node = self._nodes.get(self._focus_iid)
        if node:
            if node['kind'] == 'group':
                self.item(self._focus_iid, open=self._focus_iid in self._closed)
            else:
                self.selection_set(self._focus_iid)
                self.app.show_product_details()
        return 'break'

    def _select_all(self, event):
        self._active_cell = None
        self.selection_set([i for i in self._flat if self._nodes[i]['kind'] == 'option'])
        return 'break'
