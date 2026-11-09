"""Independent task windows placed on the main window's monitor."""
import os
import tkinter as tk
import weakref


def exists(widget):
    try:
        return widget is not None and bool(widget.winfo_exists())
    except tk.TclError:
        return False


def centered_rect(parent, size, bounds, offset=0):
    px, py, pw, ph = parent
    width, height = size
    left, top, right, bottom = bounds
    return (max(left, min(px + (pw-width)//2 + offset, right-width)),
            max(top, min(py + (ph-height)//2 + offset, bottom-height)))


def place(win, parent):
    if not exists(win) or not exists(parent):
        return
    win.update_idletasks()
    windows = getattr(parent, '_task_windows', None)
    if windows is None:
        windows = parent._task_windows = weakref.WeakSet()
    offset = 24 * (sum(exists(w) for w in windows if w is not win) % 5)
    windows.add(win)
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        class MONITORINFO(ctypes.Structure):
            _fields_ = [('cbSize', wintypes.DWORD), ('monitor', wintypes.RECT),
                        ('work', wintypes.RECT), ('flags', wintypes.DWORD)]
        api = ctypes.windll.user32
        api.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        api.GetAncestor.restype = wintypes.HWND
        api.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        api.MonitorFromWindow.restype = wintypes.HANDLE
        api.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
        api.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        api.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        hwnd = api.GetAncestor(win.winfo_id(), 2)
        owner = api.GetAncestor(parent.winfo_id(), 2)
        info = MONITORINFO(); info.cbSize = ctypes.sizeof(info)
        wr, pr = wintypes.RECT(), wintypes.RECT()
        if (api.GetMonitorInfoW(api.MonitorFromWindow(owner, 2), ctypes.byref(info))
                and api.GetWindowRect(hwnd, ctypes.byref(wr))
                and api.GetWindowRect(owner, ctypes.byref(pr))):
            x, y = centered_rect((pr.left, pr.top, pr.right-pr.left, pr.bottom-pr.top),
                (wr.right-wr.left, wr.bottom-wr.top),
                (info.work.left, info.work.top, info.work.right, info.work.bottom), offset)
            api.SetWindowPos(hwnd, None, x, y, 0, 0, 0x0015)
            return
    x, y = centered_rect((parent.winfo_rootx(), parent.winfo_rooty(), parent.winfo_width(), parent.winfo_height()),
                         (win.winfo_width(), win.winfo_height()),
                         (parent.winfo_vrootx(), parent.winfo_vrooty(),
                          parent.winfo_vrootx()+parent.winfo_vrootwidth(),
                          parent.winfo_vrooty()+parent.winfo_vrootheight()), offset)
    win.geometry(f'+{x}+{y}')


def when_closed(dialog, callback):
    """Resume this dialog's operation without nesting Tk's event loop."""
    parent = dialog.master
    def destroyed(event):
        if event.widget is dialog and exists(parent):
            parent.after_idle(callback)
    dialog.bind('<Destroy>', destroyed, add='+')
