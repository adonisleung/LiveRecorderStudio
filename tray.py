# -*- coding: utf-8 -*-
"""窗口隐藏与托盘：纯 ctypes + Win32，把 MinimizeToTray 的思路内嵌进来。

两种「藏起来」用途不同：
- 快捷键隐藏（Alt+F1 隐藏 / Alt+F2 还原）：整体藏掉，**不出托盘图标**，只靠热键召回。
- 缩到托盘（关窗时可选）：藏窗口 + 托盘图标，点图标还原，右键菜单可退出。

热键按顺序试，Alt+F1 / Alt+F2 被占用会自动换备用组合；隐藏键全被占用才挂托盘兜底。
消息窗口与消息循环跑在独立线程，不影响主线程的 WebView 循环。
"""
from __future__ import annotations

import ctypes
import re
import threading
from ctypes import wintypes

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32
kernel32 = ctypes.windll.kernel32

WM_HOTKEY = 0x0312
WM_TRAY = 0x8000 + 1
WM_REBIND = 0x8000 + 2
WM_DESTROY = 0x0002
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
VK_F1, VK_F2 = 0x70, 0x71

SW_HIDE, SW_SHOW, SW_RESTORE = 0, 5, 9

NIM_ADD, NIM_DELETE = 0, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4

IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x10, 0x40
IDI_APPLICATION = 32512
MF_STRING, MF_SEPARATOR = 0x0, 0x800
TPM_RIGHTBUTTON, TPM_RETURNCMD, TPM_BOTTOMALIGN, TPM_NONOTIFY = 0x2, 0x100, 0x20, 0x80

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class GUID(ctypes.Structure):
    _fields_ = [('Data1', wintypes.DWORD), ('Data2', wintypes.WORD),
                ('Data3', wintypes.WORD), ('Data4', ctypes.c_ubyte * 8)]


class WINDOWPLACEMENT(ctypes.Structure):
    """隐藏前记下窗口位置/大小/最大化状态，还原时原样放回去。"""
    _fields_ = [('length', wintypes.UINT), ('flags', wintypes.UINT),
                ('showCmd', wintypes.UINT),
                ('ptMinPosition', wintypes.POINT), ('ptMaxPosition', wintypes.POINT),
                ('rcNormalPosition', wintypes.RECT)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ('cbSize', wintypes.DWORD), ('hWnd', wintypes.HWND), ('uID', wintypes.UINT),
        ('uFlags', wintypes.UINT), ('uCallbackMessage', wintypes.UINT),
        ('hIcon', wintypes.HICON), ('szTip', wintypes.WCHAR * 128),
        ('dwState', wintypes.DWORD), ('dwStateMask', wintypes.DWORD),
        ('szInfo', wintypes.WCHAR * 256), ('uVersion', wintypes.UINT),
        ('szInfoTitle', wintypes.WCHAR * 64), ('dwInfoFlags', wintypes.DWORD),
        ('guidItem', GUID), ('hBalloonIcon', wintypes.HICON),
    ]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [('style', wintypes.UINT), ('lpfnWndProc', ctypes.c_void_p),
                ('cbClsExtra', ctypes.c_int), ('cbWndExtra', ctypes.c_int),
                ('hInstance', wintypes.HINSTANCE), ('hIcon', wintypes.HICON),
                ('hCursor', wintypes.HANDLE), ('hbrBackground', wintypes.HBRUSH),
                ('lpszMenuName', wintypes.LPCWSTR), ('lpszClassName', wintypes.LPCWSTR)]


class POINT(ctypes.Structure):
    _fields_ = [('x', wintypes.LONG), ('y', wintypes.LONG)]


# ctypes 默认把返回值当 32 位 int、把 LPARAM 当 int：64 位下句柄会被截断、
# 参数会溢出（OverflowError: int too long to convert），必须显式声明原型。
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.LoadIconW.restype = wintypes.HICON
user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
user32.LoadImageW.restype = wintypes.HICON
user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                              ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.TrackPopupMenu.restype = ctypes.c_int
user32.RegisterHotKey.restype = wintypes.BOOL
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetMessageW.restype = ctypes.c_int
shell32.Shell_NotifyIconW.restype = wintypes.BOOL


def _int_resource(value: int):
    return ctypes.cast(ctypes.c_void_p(value), wintypes.LPCWSTR)


HIDE_KEYS = [
    (MOD_ALT | MOD_NOREPEAT, VK_F1, 'Alt+F1'),
    (MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, 0x48, 'Ctrl+Alt+H'),
    (MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, 0x4C, 'Ctrl+Alt+L'),
    (MOD_ALT | MOD_NOREPEAT, 0xC0, 'Alt+`'),
]
SHOW_KEYS = [
    (MOD_ALT | MOD_NOREPEAT, VK_F2, 'Alt+F2'),
    (MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, 0x53, 'Ctrl+Alt+S'),
    (MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, 0x4F, 'Ctrl+Alt+O'),
    (MOD_SHIFT | MOD_ALT | MOD_NOREPEAT, VK_F2, 'Alt+Shift+F2'),
]

_NAME_OF_VK: dict[int, str] = {}
for _i in range(1, 25):
    _NAME_OF_VK[0x6F + _i] = f'F{_i}'
for _c in range(ord('A'), ord('Z') + 1):
    _NAME_OF_VK[_c] = chr(_c)
for _d in range(ord('0'), ord('9') + 1):
    _NAME_OF_VK[_d] = chr(_d)
_NAME_OF_VK.update({
    0x20: 'Space', 0x09: 'Tab', 0x0D: 'Enter', 0x1B: 'Esc', 0x08: 'Backspace',
    0x2E: 'Delete', 0x2D: 'Insert', 0x24: 'Home', 0x23: 'End',
    0x21: 'PageUp', 0x22: 'PageDown', 0x26: 'Up', 0x28: 'Down',
    0x25: 'Left', 0x27: 'Right',
    0xC0: '`', 0xBD: '-', 0xBB: '=', 0xDB: '[', 0xDD: ']', 0xBA: ';',
    0xDE: "'", 0xBC: ',', 0xBE: '.', 0xBF: '/', 0xDC: '\\',
})
_VK_OF_NAME = {v.upper(): k for k, v in _NAME_OF_VK.items()}


def format_hotkey(mods: int, vk: int) -> str:
    """(修饰键, 虚拟键码) → 'Ctrl+Alt+H' 这样的可读文本。"""
    parts = []
    if mods & MOD_CONTROL:
        parts.append('Ctrl')
    if mods & MOD_ALT:
        parts.append('Alt')
    if mods & MOD_SHIFT:
        parts.append('Shift')
    if mods & MOD_WIN:
        parts.append('Win')
    parts.append(_NAME_OF_VK.get(vk, f'VK{vk:02X}'))
    return '+'.join(parts)


def parse_hotkey(text: str):
    """'ctrl+alt+h' → (mods, vk)；无法识别返回 None。"""
    if not text:
        return None
    mods, key = 0, ''
    for part in str(text).split('+'):
        p = part.strip()
        if not p:
            continue
        low = p.lower()
        if low in ('ctrl', 'control'):
            mods |= MOD_CONTROL
        elif low == 'alt':
            mods |= MOD_ALT
        elif low == 'shift':
            mods |= MOD_SHIFT
        elif low in ('win', 'meta', 'super'):
            mods |= MOD_WIN
        else:
            key = p
    vk = _VK_OF_NAME.get(key.upper())
    if not key or not vk:
        return None
    return mods | MOD_NOREPEAT, vk


class WindowHooks:
    """快捷键隐藏/还原 + 可选托盘图标。"""

    def __init__(self, title: str, on_quit=None, tip: str = 'LiveRecorder Studio',
                 icon_path=None, hide_hotkey: str = '', show_hotkey: str = ''):
        self.title = title
        self.on_quit = on_quit
        self.tip = tip[:120]
        self.icon_path = icon_path
        self.hide_key = ''          # 实际生效的「隐藏」组合键
        self.show_key = ''          # 实际生效的「还原」组合键
        self.want_hide = hide_hotkey or ''
        self.want_show = show_hotkey or ''
        self.used_tray = False      # 隐藏键注册失败时才会挂托盘兜底
        self._hwnd_msg = None
        self._proc_ref = None
        self._cls = f'LRS_HookWnd_{id(self)}'
        self._running = False
        self._hidden = False
        self._placement = None      # 隐藏前的窗口位置，还原时放回原处
        self._last_hwnd = 0        # hide() 记下的真实窗口句柄，show() 直接用
        self._icon = None
        self._icon_added = False
        self._thread = None
        self._ready = threading.Event()
        self._lock = threading.RLock()
        self._pending = None
        self._rebind_result = None
        self._rebind_done = threading.Event()

    # ---------- 对外 ----------
    def start(self, wait: float = 2.0):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name='win-hooks')
        self._thread.start()
        self._ready.wait(wait)      # 等热键注册结果，界面才能报出组合键

    def stop(self):
        self._running = False
        if self._hwnd_msg:
            try:
                for hid in (1, 2):
                    user32.UnregisterHotKey(self._hwnd_msg, hid)
                self._del_icon()
                user32.PostMessageW(self._hwnd_msg, WM_DESTROY, 0, 0)
            except Exception:
                pass

    def hide(self) -> bool:
        """整体隐藏：窗口消失，托盘也不留图标（只能靠还原热键召回）。"""
        hwnd = self._find_main()
        if not hwnd:
            return False
        # 记下隐藏前的位置/大小/是否最大化，还原时原样放回
        pl = WINDOWPLACEMENT()
        pl.length = ctypes.sizeof(WINDOWPLACEMENT)
        if user32.GetWindowPlacement(hwnd, ctypes.byref(pl)):
            self._placement = pl
        user32.ShowWindow(hwnd, SW_HIDE)
        self._hidden = True
        self._last_hwnd = hwnd       # 记住真窗口句柄，show() 直接用
        self._del_icon()
        return True

    def show(self) -> bool:
        # 优先用 hide() 记下的句柄（已隐藏时 EnumWindows 找不到可见的）
        hwnd = self._last_hwnd or self._find_main()
        if not hwnd:
            return False
        user32.ShowWindow(hwnd, SW_SHOW)
        if self._placement is not None:
            # 用隐藏前的 placement 还原：位置、大小、最大化状态都和走之前一致
            pl = self._placement
            pl.length = ctypes.sizeof(WINDOWPLACEMENT)
            user32.SetWindowPlacement(hwnd, ctypes.byref(pl))
            self._placement = None
        else:
            user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
        self._hidden = False
        self._del_icon()
        return True

    def to_tray(self) -> bool:
        """缩到托盘：隐藏窗口 + 挂托盘图标（左键还原，右键菜单可退出）。"""
        ok = self.hide()
        if ok:
            self.used_tray = True
            if not self._icon:
                self._icon = self._load_icon()
            self._add_icon()
        return ok

    # ---------- 内部 ----------
    def _find_main(self):
        """精确找主窗口：PID + 标题匹配，优先可见窗口。

        FindWindowW(None, title) 在 WebView2 环境下不可靠——pywebview 会创建
        多个同名隐藏辅助窗口，FindWindowW 按 Z-order 找到的第一个可能是隐藏的
        假窗口，ShowWindow(SW_HIDE) 对它操作等于没隐藏真窗口。
        这里用 EnumWindows 遍历「本进程 + 标题匹配」的窗口，优先返回可见的；
        窗口已隐藏时（hide 之后），降级返回隐藏的真窗口（用于 show 还原）。
        """
        pid = kernel32.GetCurrentProcessId()
        visible_hwnd = 0
        hidden_hwnd = 0

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def _enum_cb(hwnd, _lparam):
            nonlocal visible_hwnd, hidden_hwnd
            lp_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(lp_pid))
            if lp_pid.value != pid:
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if buf.value == self.title:
                if user32.IsWindowVisible(hwnd):
                    visible_hwnd = hwnd
                    return False    # 找到可见的立即停
                hidden_hwnd = hwnd  # 记住隐藏的做备用
            return True

        user32.EnumWindows(_enum_cb, 0)
        return visible_hwnd or hidden_hwnd or user32.FindWindowW(None, self.title)

    def _run(self):
        try:
            self._create_window()
            self._register_hotkeys()
        except Exception:
            self._running = False
        finally:
            self._ready.set()
        if not self._running:
            return
        msg = wintypes.MSG()
        while self._running and user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _create_window(self):
        hinst = kernel32.GetModuleHandleW(None)
        self._proc_ref = WNDPROC(self._wndproc)
        wc = WNDCLASSW()
        wc.lpfnWndProc = ctypes.cast(self._proc_ref, ctypes.c_void_p)
        wc.hInstance = hinst
        wc.lpszClassName = self._cls
        user32.RegisterClassW(ctypes.byref(wc))
        self._hwnd_msg = user32.CreateWindowExW(
            0, self._cls, 'LRS Hooks', 0, 0, 0, 0, 0, None, None, hinst, None)
        if not self._hwnd_msg:
            raise OSError('创建消息窗口失败')

    def _register_hotkeys(self):
        self._apply_keys(self.want_hide, self.want_show)

    def rebind(self, hide_text: str, show_text: str) -> dict:
        """运行时换键。

        RegisterHotKey 必须回到创建消息窗口的那个线程执行——同一个 ID
        跨线程重注册会直接失败（实测两个键都注册不上），所以这里投递消息，
        由钩子线程完成注册后再把结果回传。
        """
        self.want_hide, self.want_show = hide_text or '', show_text or ''
        if not self._hwnd_msg:
            return {'hide': '', 'show': '', 'want_hide': self.want_hide,
                    'want_show': self.want_show}
        with self._lock:
            self._pending = (self.want_hide, self.want_show)
            self._rebind_result = None
            self._rebind_done.clear()
        user32.PostMessageW(self._hwnd_msg, WM_REBIND, 0, 0)
        if not self._rebind_done.wait(3.0):
            return {'hide': self.hide_key, 'show': self.show_key,
                    'want_hide': self.want_hide, 'want_show': self.want_show,
                    'error': '切换超时'}
        return self._rebind_result or {'hide': self.hide_key, 'show': self.show_key,
                                       'want_hide': self.want_hide,
                                       'want_show': self.want_show}

    def suspend(self) -> dict:
        """录入新快捷键期间卸掉全局热键。

        不卸不行：Alt+F1/F2 本身是全局热键，用户在输入框按它会被系统直接吃掉
        （还会把窗口藏了），永远录不进去。失焦/保存后调 resume 恢复。
        """
        with self._lock:
            self._pending = ('__suspend__', '__suspend__')
            self._rebind_result = None
            self._rebind_done.clear()
        user32.PostMessageW(self._hwnd_msg, WM_REBIND, 0, 0)
        self._rebind_done.wait(3.0)
        return {'ok': True, 'hide': '', 'show': '', 'suspended': True}

    def resume(self) -> dict:
        """恢复被 suspend 卸掉的全局热键（用最近一次想要的组合）。"""
        res = self.rebind(self.want_hide, self.want_show)
        res['ok'] = True
        return res

    def _apply_keys(self, hide_text: str, show_text: str) -> dict:
        for hid in (1, 2):
            user32.UnregisterHotKey(self._hwnd_msg, hid)
        if hide_text == '__suspend__':      # 录入期间：两个键都不注册，按键直达输入框
            self.hide_key = self.show_key = ''
            return {'hide': '', 'show': '', 'want_hide': self.want_hide,
                    'want_show': self.want_show, 'suspended': True}
        self.hide_key = self._try_bind(1, hide_text, HIDE_KEYS)
        self.show_key = self._try_bind(2, show_text, SHOW_KEYS)
        if not self.hide_key:
            # 连隐藏键都注册不上：挂托底图标，别让窗口被藏死找不回来
            self.used_tray = True
            if not self._icon:
                self._icon = self._load_icon()
            self._add_icon()
        return {'hide': self.hide_key, 'show': self.show_key,
                'want_hide': hide_text, 'want_show': show_text}

    def _try_bind(self, hid: int, text: str, defaults) -> str:
        cands = []
        parsed = parse_hotkey(text)
        if parsed:
            cands.append((parsed[0], parsed[1], format_hotkey(*parsed)))
        taken = {(m & ~MOD_NOREPEAT, v) for m, v, _ in cands}
        for mods, vk, name in defaults:
            if (mods & ~MOD_NOREPEAT, vk) in taken:
                continue
            cands.append((mods, vk, name))
        for mods, vk, name in cands:
            if user32.RegisterHotKey(self._hwnd_msg, hid, mods, vk):
                return name
        return ''

    def _load_icon(self):
        try:
            if self.icon_path and self.icon_path.is_file():
                h = user32.LoadImageW(None, str(self.icon_path), IMAGE_ICON, 0, 0,
                                      LR_LOADFROMFILE | LR_DEFAULTSIZE)
                if h:
                    return h
        except Exception:
            pass
        return user32.LoadIconW(None, _int_resource(IDI_APPLICATION))

    def _nid(self):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd_msg
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self._icon
        nid.szTip = self.tip
        return nid

    def _add_icon(self):
        if self._icon_added or not self._hwnd_msg:
            return
        if not self._icon:
            self._icon = self._load_icon()
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._nid()))
        self._icon_added = True

    def _del_icon(self):
        if not self._icon_added or not self._hwnd_msg:
            return
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid()))
        self._icon_added = False

    def _popup(self):
        pt = POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        hmenu = user32.CreatePopupMenu()
        user32.AppendMenuW(hmenu, MF_STRING, 1, '显示主窗口')
        user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(hmenu, MF_STRING, 2, '退出程序')
        user32.SetForegroundWindow(self._hwnd_msg)
        cmd = user32.TrackPopupMenu(
            hmenu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY | TPM_BOTTOMALIGN,
            pt.x, pt.y, 0, self._hwnd_msg, None)
        user32.DestroyMenu(hmenu)
        if cmd == 1:
            self.show()
        elif cmd == 2:
            self._quit()

    def _quit(self):
        try:
            if self.on_quit:
                self.on_quit()
        finally:
            self._running = False

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_HOTKEY:
                hid = int(wparam)
                if hid == 1:
                    self.hide()
                elif hid == 2:
                    self.show()
                return 0
            if msg == WM_TRAY:
                ev = int(lparam) & 0xFFFF
                if ev in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    self.show()
                elif ev == WM_RBUTTONUP:
                    self._popup()
                return 0
            if msg == WM_REBIND:
                with self._lock:
                    args = self._pending or (self.want_hide, self.want_show)
                    self._rebind_result = self._apply_keys(*args)
                self._rebind_done.set()
                return 0
            if msg == WM_DESTROY:
                self._del_icon()
                if self._hwnd_msg:
                    for hid in (1, 2):
                        user32.UnregisterHotKey(self._hwnd_msg, hid)
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            pass
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
