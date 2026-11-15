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
import os
import re
import threading
import time
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
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.GetWindowLongW.restype = ctypes.c_long
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
user32.SetWindowPos.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.GetMessageW.restype = ctypes.c_int
shell32.Shell_NotifyIconW.restype = wintypes.BOOL
# 前台锁绕过：x64 下不声明 argtypes 会把 HWND 截成 32 位，必须显式声明
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                            ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.AttachThreadInput.restype = wintypes.BOOL
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.BringWindowToTop.restype = wintypes.BOOL
user32.SwitchToThisWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
kernel32.GetCurrentThreadId.restype = wintypes.DWORD


def _hk_log(msg: str):
    """热键链路诊断日志：出问题时读 %TEMP%\\lrs_hotkey.log，不用再猜。"""
    try:
        import tempfile
        with open(os.path.join(tempfile.gettempdir(), 'lrs_hotkey.log'),
                  'a', encoding='utf-8') as f:
            f.write(f'{time.strftime("%H:%M:%S")} {msg}\n')
    except Exception:
        pass


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
                 icon_path=None, hide_hotkey: str = '', show_hotkey: str = '',
                 import_hotkey: str = ''):
        self.title = title
        self.on_quit = on_quit
        self.tip = tip[:120]
        self.icon_path = icon_path
        self.hide_key = ''          # 实际生效的「隐藏」组合键
        self.show_key = ''          # 实际生效的「还原」组合键
        self.import_key = ''        # 实际生效的「导入」组合键
        self.want_hide = hide_hotkey or ''
        self.want_show = show_hotkey or ''
        self.want_import = import_hotkey or ''
        self.import_requested = False  # 导入热键触发标记，前端轮询消费
        self.import_from_hidden = False  # 导入前窗口是否处于隐藏状态
        self._saved_style = 0        # 去边框前保存的窗口样式
        self._saved_rect = None      # 缩小前保存的窗口位置大小
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
        # 窗口创建后缓存一次真实句柄。_wndproc（消息循环线程）里绝不能做
        # EnumWindows：GetWindowTextW 会跨线程 SendMessage 到 GUI 线程，
        # GUI 线程忙时会把热键消息循环堵死（历史上「最小化热键卡死」）。
        threading.Thread(target=self._cache_hwnd, daemon=True,
                         name='lrs-hwnd-cache').start()

    def _cache_hwnd(self):
        """后台重试拿主窗口句柄；窗口刚创建时标题/句柄可能还没就绪。"""
        for _ in range(10):
            time.sleep(0.8)
            if not self._running:
                return
            hwnd = self._find_main()
            if hwnd:
                self._last_hwnd = hwnd
                return

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
        hwnd = self._last_hwnd or self._find_main()
        if not hwnd:
            return False
        # 缓存真实窗口句柄：最小化检测也要用它。以前只有 hide() 才写这个字段，
        # 用户点任务栏最小化时它恒为 0，IsIconic(0) 自然永远为假。
        self._last_hwnd = hwnd
        # 最小化时必须先 SW_RESTORE 才能正常显示和置前
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        elif self._placement is not None:
            pl = self._placement
            pl.length = ctypes.sizeof(WINDOWPLACEMENT)
            user32.SetWindowPlacement(hwnd, ctypes.byref(pl))
            self._placement = None
            user32.ShowWindow(hwnd, SW_SHOW)
        else:
            user32.ShowWindow(hwnd, SW_SHOW)
        self._force_foreground(hwnd)
        self._hidden = False
        self._del_icon()
        return True

    @staticmethod
    def _force_foreground(hwnd) -> None:
        """置前：只用非阻塞的标准 API。

        show() 是在热键消息循环线程里同步调用的，绝不能在这里做
        AttachThreadInput / SwitchToThisWindow / TOPMOST 翻转这类跨线程
        同步操作 —— 隐藏态恢复时会等 GUI 线程，可能造成界面互锁（用户
        实测「按 Alt+F2 后卡死、关不掉」，进程 Responding 仍为 True）。
        窗口的显示由 ShowWindow 完成，这里只补一个置前，失败就算了。
        """
        try:
            user32.SetForegroundWindow(hwnd)
        except Exception:
            pass

    def set_borderless(self) -> bool:
        """去掉标题栏并缩成小弹窗（居中显示），视觉上独立于主窗口。"""
        hwnd = self._last_hwnd or self._find_main()
        if not hwnd:
            return False
        GWL_STYLE = -16
        WS_CAPTION = 0x00C00000
        WS_THICKFRAME = 0x00040000
        style = user32.GetWindowLongW(hwnd, GWL_STYLE)
        self._saved_style = style
        self._saved_rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(self._saved_rect))
        user32.SetWindowLongW(hwnd, GWL_STYLE, style & ~(WS_CAPTION | WS_THICKFRAME))
        # 缩成小弹窗居中
        import ctypes.wintypes as wt
        sw = user32.GetSystemMetrics(0)  # 屏宽
        sh = user32.GetSystemMetrics(1)  # 屏高
        w, h = 480, 380
        x, y = (sw - w) // 2, (sh - h) // 2
        user32.SetWindowPos(hwnd, 0, x, y, w, h,
                            0x0002 | 0x0004 | 0x0020)  # NOZORDER|NOACTIVATE|FRAMECHANGED
        return True

    def restore_border(self) -> bool:
        """恢复标题栏和原始窗口大小。"""
        hwnd = self._last_hwnd or self._find_main()
        if not hwnd:
            return False
        GWL_STYLE = -16
        if self._saved_style:
            user32.SetWindowLongW(hwnd, GWL_STYLE, self._saved_style)
            self._saved_style = 0
        if self._saved_rect:
            r = self._saved_rect
            user32.SetWindowPos(hwnd, 0, r.left, r.top, r.right - r.left, r.bottom - r.top,
                                0x0002 | 0x0004 | 0x0020)
            self._saved_rect = None
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
        self._apply_keys(self.want_hide, self.want_show, self.want_import)

    def rebind(self, hide_text: str, show_text: str,
               import_text: str | None = None) -> dict:
        self.want_hide, self.want_show = hide_text or '', show_text or ''
        # 「不传第三个参数」= 保留原来的导入键，绝不能无条件清空。
        # 旧版这里写 self.want_import = import_text or ''，而 resume() 只传
        # hide/show 两个参数：每次导入框里的快捷键输入框失焦（suspend→resume），
        # Alt+1 就被永久注销，直到重启或改键才回来。表现就是「用过一次导入框
        # 之后，Alt+1 再无反应，但 Alt+F1/F2 一直正常」。
        if import_text is not None:
            self.want_import = import_text or ''
        if not self._hwnd_msg:
            return {'hide': '', 'show': '', 'import': '', 'want_hide': self.want_hide,
                    'want_show': self.want_show, 'want_import': self.want_import}
        with self._lock:
            self._pending = (self.want_hide, self.want_show, self.want_import)
            self._rebind_result = None
            self._rebind_done.clear()
        user32.PostMessageW(self._hwnd_msg, WM_REBIND, 0, 0)
        if not self._rebind_done.wait(3.0):
            return {'hide': self.hide_key, 'show': self.show_key, 'import': self.import_key,
                    'want_hide': self.want_hide, 'want_show': self.want_show,
                    'want_import': self.want_import, 'error': '切换超时'}
        return self._rebind_result or {'hide': self.hide_key, 'show': self.show_key,
                                       'import': self.import_key,
                                       'want_hide': self.want_hide,
                                       'want_show': self.want_show,
                                       'want_import': self.want_import}

    def suspend(self) -> dict:
        """录入新快捷键期间卸掉全局热键。

        不卸不行：Alt+F1/F2 本身是全局热键，用户在输入框按它会被系统直接吃掉
        （还会把窗口藏了），永远录不进去。失焦/保存后调 resume 恢复。
        """
        with self._lock:
            # 带上导入键，避免 _pending 退化成二元组后 _apply_keys 少收一个参数
            self._pending = ('__suspend__', '__suspend__', self.want_import)
            self._rebind_result = None
            self._rebind_done.clear()
        user32.PostMessageW(self._hwnd_msg, WM_REBIND, 0, 0)
        self._rebind_done.wait(3.0)
        return {'ok': True, 'hide': '', 'show': '', 'suspended': True}

    def resume(self) -> dict:
        """恢复被 suspend 卸掉的全局热键（三个键都要，别漏了导入键）。"""
        res = self.rebind(self.want_hide, self.want_show, self.want_import)
        res['ok'] = True
        return res

    def _apply_keys(self, hide_text: str, show_text: str, import_text: str = '') -> dict:
        for hid in (1, 2, 3):
            user32.UnregisterHotKey(self._hwnd_msg, hid)
        if hide_text == '__suspend__':      # 录入期间：三个键都不注册，按键直达输入框
            self.hide_key = self.show_key = self.import_key = ''
            _hk_log('keys SUSPENDED (all unregistered)')
            return {'hide': '', 'show': '', 'want_hide': self.want_hide,
                    'want_show': self.want_show, 'suspended': True}
        self.hide_key = self._try_bind(1, hide_text, HIDE_KEYS)
        self.show_key = self._try_bind(2, show_text, SHOW_KEYS)
        want = import_text or self.want_import
        self.import_key = self._try_bind(3, want, []) if want else ''
        _hk_log(f'keys bound: hide={self.hide_key or "-"} '
                f'show={self.show_key or "-"} import={self.import_key or "-"} '
                f'want_import={self.want_import or "-"}')
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
                _hk_log(f'WM_HOTKEY id={hid}')
                if hid == 1:
                    self.hide()
                elif hid == 2:
                    self.show()
                elif hid == 3:
                    # 不能只靠 hide() 记下的句柄判断状态：点任务栏最小化从没
                    # 走过 hide()，_last_hwnd 恒为 0，IsIconic(0) 也就恒为假，
                    # 结果窗口不恢复，导入框弹在最小化窗口里、用户根本看不见。
                    # 只读后台缓存的句柄：这里不能调 _find_main()（见 start 注释）
                    hwnd = self._last_hwnd
                    iconic = bool(hwnd) and bool(user32.IsIconic(hwnd))
                    hidden = self._hidden or (bool(hwnd) and
                                             not user32.IsWindowVisible(hwnd))
                    # 只有显式 Alt+F1 隐藏才在关掉导入框后自动藏回去；最小化
                    # 不是隐藏，否则会变成「最小化按热键 → 关框 → 窗口消失」。
                    from_hidden = self._hidden
                    self.import_from_hidden = from_hidden
                    _hk_log(f'id=3 before: hidden={hidden} iconic={iconic} '
                            f'self_hidden={self._hidden} hwnd={hwnd} '
                            f'vis={bool(hwnd) and bool(user32.IsWindowVisible(hwnd))} '
                            f'fg={user32.GetForegroundWindow()}')
                    # 只在确实需要恢复窗口时才调 show()：可见态不碰窗口，
                    # 把跨线程 GUI 操作降到最少（消息循环里做多了会互锁）。
                    if iconic or hidden:
                        self.show()
                    _hk_log(f'id=3 after show: '
                            f'vis={bool(hwnd) and bool(user32.IsWindowVisible(hwnd))} '
                            f'iconic={bool(hwnd) and bool(user32.IsIconic(hwnd))} '
                            f'fg={user32.GetForegroundWindow()}')
                    # 走前端唯一入口 hotkeyOpenImport：导入框已开着时它会把焦点
                    # 还给输入框；直调 openImportDialog 会被 isModalOpen() 直接
                    # return，连按热键看起来就是「按了没反应」。
                    # 放独立线程只防极端情况锁死消息循环，语义不变。
                    flag = 'true' if from_hidden else 'false'
                    script = ('window._importFromHidden=%s;'
                              'typeof hotkeyOpenImport==="function"'
                              ' ? hotkeyOpenImport(%s)'
                              ' : (openImportDialog && openImportDialog())'
                              % (flag, flag))

                    def _fire(_script=script):
                        try:
                            import webview as _wv
                            if _wv.windows:
                                _wv.windows[0].evaluate_js(_script)
                                _hk_log('id=3 evaluate_js ok')
                            else:
                                _hk_log('id=3 no webview window -> poll')
                                self.import_requested = True
                        except Exception as e:
                            _hk_log(f'id=3 evaluate_js FAILED {type(e).__name__}: {e}')
                            self.import_requested = True  # 回退：走轮询
                    threading.Thread(target=_fire, daemon=True, name='lrs-import-js').start()
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
                    args = self._pending or (self.want_hide, self.want_show, self.want_import)
                    self._rebind_result = self._apply_keys(*args)
                self._rebind_done.set()
                return 0
            if msg == WM_DESTROY:
                self._del_icon()
                if self._hwnd_msg:
                    for hid in (1, 2, 3):
                        user32.UnregisterHotKey(self._hwnd_msg, hid)
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            pass
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
