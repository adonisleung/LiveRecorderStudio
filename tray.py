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
import tempfile
import threading
import time
from ctypes import wintypes

# 热键链路日志：热键是交互问题，出问题时光看代码猜不出来，
# 落到临时目录（%TEMP%\lrs_hotkey.log）里，回读就能定位卡在哪一步。
_LOG_PATH = os.path.join(tempfile.gettempdir(), 'lrs_hotkey.log')


def _log(msg: str) -> None:
    try:
        if os.path.exists(_LOG_PATH) and os.path.getsize(_LOG_PATH) > 200 * 1024:
            with open(_LOG_PATH, 'w', encoding='utf-8'):
                pass                      # 超过 200KB 直接清空，避免无限增长
        with open(_LOG_PATH, 'a', encoding='utf-8') as f:
            f.write('%s %s\n' % (time.strftime('%m-%d %H:%M:%S'), msg))
    except Exception:
        pass

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
# 窗口置前（强制前台）用到的几个 API：64 位下必须显式声明 restype/argtypes，
# 否则 HWND 会被当成 int 截断，AttachThreadInput 拿到的线程 ID 也是错的。
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.AttachThreadInput.restype = wintypes.BOOL
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.BringWindowToTop.restype = wintypes.BOOL
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
user32.RegisterHotKey.restype = wintypes.BOOL
user32.IsWindowVisible.restype = wintypes.BOOL
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


def _webview_ctrl(win):
    """取到 WebView2 控件实例，取不到返回 None。

    pywebview 6.x 里 `window.gui` 指向的是**平台模块**而不是控件，
    真正的控件挂在 `BrowserView.instances[uid].browser.webview` 上。
    拿到它才能用「投递不等待」的方式执行脚本。
    """
    try:
        from webview.platforms import winforms as _wf
        inst = _wf.BrowserView.instances.get(getattr(win, 'uid', None))
        browser = getattr(inst, 'browser', None)
        return getattr(browser, 'webview', None)
    except Exception:
        return None


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
        self._js_threads = 0        # 正在跑的推送线程数，超过上限就不再堆
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
        # 先用 pywebview 自己的窗口对象：它持有 uid→控件引用，不依赖窗口标题。
        pw = self._pw_window()
        ok_pw = False
        if pw is not None:
            try:
                pw.hide()
                ok_pw = True
            except Exception as e:
                _log('hide: pywebview failed %s' % e)

        hwnd = self._last_hwnd or self._find_main()
        if hwnd:
            self._last_hwnd = hwnd
            # 记下隐藏前的位置/大小/是否最大化，还原时原样放回
            pl = WINDOWPLACEMENT()
            pl.length = ctypes.sizeof(WINDOWPLACEMENT)
            if user32.GetWindowPlacement(hwnd, ctypes.byref(pl)):
                self._placement = pl
            user32.ShowWindow(hwnd, SW_HIDE)

        # 即使两条路都没成功也记成已隐藏：用户按的是隐藏键，
        # 下次还原时要按「已隐藏」处理，否则会走错分支。
        self._hidden = True
        self._del_icon()
        _log('hide: hwnd=%s pw=%s' % (hwnd or 0, ok_pw))
        return bool(hwnd or ok_pw)

    def show(self) -> bool:
        # 先用 pywebview 自己的窗口对象恢复：它持有 uid → 控件引用，
        # 完全不依赖窗口标题匹配（标题被页面 <title> 一改，FindWindow 就全失效，
        # 表现正是「窗口隐藏/最小化后热键叫不回来」）。
        pw = self._pw_window()
        ok_pw = False
        if pw is not None:
            try:
                pw.show()
                ok_pw = True
            except Exception as e:
                _log('show: pywebview.show failed %s' % e)
            try:
                pw.restore()        # 最小化状态要用 restore 才回得来
            except Exception:
                pass

        hwnd = self._last_hwnd or self._find_main()
        if hwnd:
            # 缓存句柄：hide/show/最小化检测/热键推送都要用。
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
        _log('show: hwnd=%s pw=%s' % (hwnd or 0, ok_pw))
        return bool(hwnd or ok_pw)

    @staticmethod
    def _force_foreground(hwnd) -> None:
        """把窗口拉到前台。

        直接调 SetForegroundWindow 在非前台进程里会被 Windows 的前台锁拒绝
        （返回 0、窗口不动），表现就是「按了热键没反应，点一下窗口才出现」。
        标准解法：临时把自己 attach 到当前前台窗口所属线程，置前后再 detach。
        """
        try:
            fg = user32.GetForegroundWindow()
            if fg == hwnd:
                return
            cur_tid = kernel32.GetCurrentThreadId()
            fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
            attached = False
            if fg_tid and fg_tid != cur_tid:
                attached = bool(user32.AttachThreadInput(cur_tid, fg_tid, True))
            try:
                user32.SetForegroundWindow(hwnd)
                user32.BringWindowToTop(hwnd)
            finally:
                if attached:
                    user32.AttachThreadInput(cur_tid, fg_tid, False)
        except Exception:
            try:
                user32.SetForegroundWindow(hwnd)
            except Exception:
                pass

    def _push_import_js(self) -> None:
        """热键后从独立线程把「打开导入框」推给前端。

        为什么不直接在 _wndproc 里调 evaluate_js：
        那是在消息循环线程里同步等待，等于「自己等自己」，窗口最小化时界面直接锁死。

        为什么允许多个推送线程并存（上限 3）：
        pywebview 的 evaluate_js 内部是 Invoke + semaphore 等待，渲染进程还没
        活过来时它不返回。之前用「一次只许一个，卡住就跳过后续」的策略，
        结果第一次卡住后，第二次第三次全被跳过 —— 正好是
        「第一次正常、第二次慢、第三次没反应」。现在改成计数上限，
        某一次卡住只影响它自己，后续热键照样能推。
        """
        if self._js_threads >= 3:
            _log('push skipped: %d threads still alive' % self._js_threads)
            return
        self._js_threads += 1
        from_hidden = 'true' if self.import_from_hidden else 'false'
        # 前端统一入口：已开着就聚焦输入框，没开才弹出（避免连按看起来没反应）
        script = ('try{hotkeyOpenImport&&hotkeyOpenImport(%s)}catch(e){};1' % from_hidden)

        def _run():
            t0 = time.time()
            try:
                # 等窗口真的离开最小化：渲染进程挂起时发脚本会一直等
                hwnd = self._last_hwnd or self._find_main() or 0
                waited = 0
                while hwnd and user32.IsIconic(hwnd) and waited < 1200:
                    time.sleep(0.1)
                    waited += 100
                time.sleep(0.10)

                import webview as _wv
                windows = getattr(_wv, 'windows', None) or []
                if not windows:
                    _log('push skipped: no window')
                    return

                win = windows[0]
                ctrl = _webview_ctrl(win)
                if ctrl is not None:
                    from System import Action

                    def _kick():
                        try:
                            ctrl.ExecuteScriptAsync(script)   # 只投递，不等结果
                        except Exception:
                            pass

                    # Invoke 只为把「投递」这一步交给 UI 线程，脚本本身异步执行；
                    # 窗口刚从隐藏/最小化恢复、渲染进程还没活时也能排队等它执行完。
                    ctrl.Invoke(Action(_kick))
                    _log('push ok (async) %.0fms waited=%dms'
                         % ((time.time() - t0) * 1000, waited))
                else:
                    win.evaluate_js(script)
                    _log('push ok (sync) %.0fms waited=%dms'
                         % ((time.time() - t0) * 1000, waited))
            except Exception as e:
                _log('push FAILED %s: %s' % (type(e).__name__, e))
            finally:
                self._js_threads = max(0, self._js_threads - 1)

        threading.Thread(target=_run, daemon=True, name='lrs-import-js').start()

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
    @staticmethod
    def _pw_window():
        """取 pywebview 的 Window 对象（内部按 uid 持有控件引用，不依赖窗口标题）。"""
        try:
            import webview as _wv
            windows = getattr(_wv, 'windows', None) or []
            return windows[0] if windows else None
        except Exception:
            return None

    def _find_main(self):
        """找主窗口：优先标题精确匹配，兜底取「同进程 + 有标题 + 面积最大」的窗口。

        原来只按标题精确匹配，太脆弱 —— WebView2 会把页面 <title> 同步成窗口标题，
        标题一有偏差（改文案、加后缀）hide/show 就全部失效，
        外部表现就是「窗口隐藏或最小化后，热键怎么也唤不回来」。
        加这层兜底后，标题对不上也能找到那个真正的大窗口。
        """
        try:
            user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
            user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
            user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
            user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        except Exception:
            pass

        def _as_int(h):
            """HWND 可能是 int 也可能是 c_void_p，统一成 int 便于日志和返回。"""
            if not h:
                return 0
            return h.value if hasattr(h, 'value') else int(h)

        pid = kernel32.GetCurrentProcessId()
        exact = 0
        best = 0
        best_area = 0
        cands = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def _enum_cb(hwnd, _lparam):
            nonlocal exact, best, best_area
            lp_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(lp_pid))
            if lp_pid.value != pid:
                return True
            cbuf = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(hwnd, cbuf, 128)
            if cbuf.value.startswith('LRS_HookWnd'):
                return True          # 自己建的消息窗口，不是主窗口
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if not exact and len(cands) < 4:
                cands.append('%x|%s|%s' % (_as_int(hwnd), cbuf.value[:14], buf.value[:20]))
            if buf.value == self.title:
                exact = _as_int(hwnd)
                return False
            r = wintypes.RECT()
            if user32.GetWindowRect(hwnd, ctypes.byref(r)):
                area = (r.right - r.left) * (r.bottom - r.top)
                if area > best_area:
                    best_area = area
                    best = _as_int(hwnd)
            return True

        user32.EnumWindows(_enum_cb, 0)
        if exact:
            return exact
        if best:
            _log('find_main: 标题未命中，按面积兜底 hwnd=%s 候选=%s'
                 % (hex(best), ' ; '.join(cands)))
            return best
        _log('find_main: 没找到任何候选窗口 cands=%s' % (' ; '.join(cands) or '无'))
        return _as_int(user32.FindWindowW(None, self.title))

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

    def rebind(self, hide_text: str, show_text: str, import_text: str = '') -> dict:
        self.want_hide, self.want_show = hide_text or '', show_text or ''
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

    def _apply_keys(self, hide_text: str, show_text: str, import_text: str = '') -> dict:
        for hid in (1, 2, 3):
            user32.UnregisterHotKey(self._hwnd_msg, hid)
        if hide_text == '__suspend__':      # 录入期间：两个键都不注册，按键直达输入框
            self.hide_key = self.show_key = self.import_key = ''
            return {'hide': '', 'show': '', 'want_hide': self.want_hide,
                    'want_show': self.want_show, 'suspended': True}
        self.hide_key = self._try_bind(1, hide_text, HIDE_KEYS)
        self.show_key = self._try_bind(2, show_text, SHOW_KEYS)
        self.import_key = self._try_bind(3, import_text or self.want_import, []) if (import_text or self.want_import) else ''
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
                _log('WM_HOTKEY id=%d' % hid)   # 先记原始消息，确认到底是哪个键
                if hid == 1:
                    self.hide()
                elif hid == 2:
                    self.show()
                elif hid == 3:
                    # 只做两件事：标记请求 + 恢复窗口，然后立刻返回。
                    # 绝对不能在消息循环里同步调 evaluate_js —— 窗口最小化时
                    # WebView2 渲染进程会被 Chromium 节流/挂起，JS 请求死等返回值，
                    # 整个消息循环被阻塞，表现就是按下快捷键后界面彻底卡死。
                    hwnd_now = self._last_hwnd or self._find_main() or 0
                    iconic = bool(user32.IsIconic(hwnd_now))
                    was_hidden = self._hidden or iconic
                    self.import_from_hidden = was_hidden
                    self.import_requested = True
                    # 不管是否隐藏都拉到前台：窗口可见但被别的程序挡住时，
                    # 不置前前端就收不到 focus/visibilitychange，只能等轮询或用户点一下。
                    t_show = time.time()
                    self.show()
                    _log('hotkey: hidden=%s iconic=%s show=%.0fms'
                         % (was_hidden, iconic, (time.time() - t_show) * 1000))
                    # 独立线程异步推一次，绕开「后台轮询被节流、必须点一下才消费」的问题
                    self._push_import_js()
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
