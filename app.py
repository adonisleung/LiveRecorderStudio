# -*- coding: utf-8 -*-
"""LiveRecorder Studio 入口：本地 HTTP 服务 + 自动打开浏览器。

零第三方依赖。运行：python app.py
"""
from __future__ import annotations

import json
import os
import re
import string
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from core import BASE, DATA_DIR, Engine, _norm_dir

MEIPASS = Path(getattr(sys, '_MEIPASS', BASE))
STATIC = MEIPASS / 'static'
if not STATIC.is_dir():
    STATIC = BASE / 'static'

# 打包后图标随 _internal 一起分发，开发时就在项目根
_ICON_CANDIDATES = (MEIPASS / 'icon.ico', BASE / 'icon.ico')
ICON = next((p for p in _ICON_CANDIDATES if p.is_file()), None)

MIME = {'.html': 'text/html; charset=utf-8',
        '.css': 'text/css; charset=utf-8',
        '.js': 'application/javascript; charset=utf-8',
        '.svg': 'image/svg+xml',
        '.ico': 'image/x-icon',
        '.mp4': 'video/mp4'}

ENGINE: Engine | None = None
SERVER = None


def list_dirs(target: str) -> dict:
    """给界面用的目录浏览：空路径返回盘符列表，否则返回该目录下的子目录。"""
    raw = (target or '').strip().strip('"')
    if not raw:
        drives = []
        if os.name == 'nt':
            for ch in string.ascii_uppercase:
                root = f'{ch}:\\'
                if os.path.isdir(root):
                    drives.append({'name': root, 'path': root})
        else:
            drives.append({'name': '/', 'path': '/'})
        return {'path': '', 'parent': '', 'dirs': drives, 'root': True}

    p = _norm_dir(raw)          # 手输 'G：' 等变体也要能跳对盘
    if not p.is_dir():
        return {'path': str(p), 'parent': str(p.parent), 'dirs': [], 'error': '目录不存在'}
    dirs = []
    try:
        with os.scandir(p) as it:
            for e in it:
                try:
                    if e.is_dir():
                        dirs.append({'name': e.name, 'path': str(p / e.name)})
                except OSError:
                    continue
    except OSError as e:
        return {'path': str(p), 'parent': str(p.parent), 'dirs': [], 'error': f'无法读取：{e}'}
    dirs.sort(key=lambda x: x['name'].lower())
    parent = str(p.parent) if p.parent != p else ''
    return {'path': str(p), 'parent': parent, 'dirs': dirs[:600], 'root': False}


def clipboard_text() -> str:
    """读本机剪贴板里的文字（CF_UNICODETEXT）。

    前端的 navigator.clipboard.readText() 在 WebView2 里要过权限，容易弹
    「允许访问剪贴板？」的原生提示；改由本地进程直接读系统剪贴板，
    既不弹窗，也不受浏览器权限模型影响。
    """
    if os.name != 'nt':
        return ''
    import ctypes
    CF_UNICODETEXT = 13
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    # 64 位进程里句柄是指针：不声明 restype/argtypes 会被截成 32 位，读出来永远是空
    u.OpenClipboard.argtypes = [ctypes.c_void_p]
    u.GetClipboardData.restype = ctypes.c_void_p
    u.GetClipboardData.argtypes = [ctypes.c_uint]
    k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalLock.argtypes = [ctypes.c_void_p]
    k.GlobalUnlock.argtypes = [ctypes.c_void_p]
    if not u.OpenClipboard(None):
        return ''
    try:
        h = u.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return ''
        p = k.GlobalLock(h)
        if not p:
            return ''
        try:
            return ctypes.wstring_at(p)
        finally:
            k.GlobalUnlock(h)
    except Exception:
        return ''
    finally:
        u.CloseClipboard()


def pick_directory(title: str = '选择录制文件的保存位置') -> dict:
    """弹系统原生「选择文件夹」对话框（ctypes 直调 shell32，零第三方依赖）。

    对齐 StreamCap 那类 Flet 应用 FilePicker.get_directory_path() 的做法：
    用系统对话框选目录，比自绘目录树快得多，也自带网络路径/新建文件夹。
    取消返回 {'ok': False, 'cancel': True}，前端据此静默返回、回退内置选择器。
    """
    if os.name != 'nt':
        return {'ok': False, 'error': '仅支持 Windows'}
    import ctypes
    from ctypes import wintypes

    BIF_RETURNONLYFSDIRS = 0x0001
    BIF_NEWDIALOGSTYLE = 0x0040
    BIF_EDITBOX = 0x0010

    class BROWSEINFOW(ctypes.Structure):
        _fields_ = [('hwndOwner', wintypes.HWND),
                    ('pidlRoot', ctypes.c_void_p),
                    ('pszDisplayName', wintypes.LPWSTR),
                    ('lpszTitle', wintypes.LPCWSTR),
                    ('ulFlags', wintypes.UINT),
                    ('lpfn', ctypes.c_void_p),
                    ('lParam', wintypes.LPARAM),
                    ('iImage', ctypes.c_int)]

    try:
        ctypes.windll.ole32.CoInitialize(None)
    except Exception:
        pass

    shell32 = ctypes.windll.shell32
    # 64 位下不声明 restype/argtypes，指针会被截断成 int 直接崩，必须显式声明
    shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
    shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BROWSEINFOW)]
    shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
    ctypes.windll.ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]

    name = ctypes.create_unicode_buffer(260)
    bi = BROWSEINFOW()
    bi.hwndOwner = None
    bi.pidlRoot = None
    bi.pszDisplayName = ctypes.cast(name, wintypes.LPWSTR)
    bi.lpszTitle = title
    bi.ulFlags = BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE | BIF_EDITBOX

    pidl = shell32.SHBrowseForFolderW(ctypes.byref(bi))
    if not pidl:
        return {'ok': False, 'cancel': True}
    out = ctypes.create_unicode_buffer(260)
    got = shell32.SHGetPathFromIDListW(pidl, out)
    ctypes.windll.ole32.CoTaskMemFree(pidl)
    if not got or not out.value:
        return {'ok': False, 'error': '没有取到路径'}
    return {'ok': True, 'path': out.value}


def _quit_trace(msg: str):
    try:
        logdir = DATA_DIR / 'logs'
        logdir.mkdir(parents=True, exist_ok=True)
        p = logdir / 'quit.log'
        if p.is_file() and p.stat().st_size > 64 * 1024:
            p.unlink()
        with open(p, 'a', encoding='utf-8') as f:
            f.write(f'{time.strftime("%H:%M:%S")} {msg}\n')
    except Exception:
        pass


def request_quit(wait: float = 0.1):
    """界面里点「退出程序」时调用：限时收尾录制，然后直接退出进程。

    走 halt_and_finalize 而不是 stop()：后者的逐进程等待会让退出拖到十几秒。
    """
    def run():
        _quit_trace('quit thread started')
        try:
            if ENGINE:
                ENGINE.halt_and_finalize()
        except Exception as e:
            _quit_trace(f'halt error: {type(e).__name__}: {e}')
        _quit_trace('halt done, sleeping')
        time.sleep(wait)
        _quit_trace('calling os._exit')
        os._exit(0)
    th = threading.Thread(target=run, daemon=True, name='quit')
    th.start()
    _quit_trace(f'thread alive={th.is_alive()}')


def make_handler(engine: Engine):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'LiveRecorderStudio/1.0'

        def log_message(self, fmt, *args):
            pass  # 不把每次请求刷到控制台

        def _json(self, data, code=200):
            body = json.dumps(data, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def _file(self, rel: str):
            name = 'index.html' if rel in ('', '/') else rel.lstrip('/')
            path = (STATIC / name).resolve()
            if not str(path).startswith(str(STATIC.resolve())) or not path.is_file():
                self._json({'error': 'not found'}, 404)
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', MIME.get(path.suffix, 'application/octet-stream'))
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            try:
                n = int(self.headers.get('Content-Length') or 0)
                return json.loads(self.rfile.read(n).decode('utf-8')) if n else {}
            except Exception:
                return {}

        def _serve_file(self, path: Path, mime: str | None = None):
            if not path.is_file():
                self._json({'error': 'not found'}, 404)
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', mime or MIME.get(path.suffix, 'application/octet-stream'))
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path
            if path == '/api/state':
                data = engine.state()
                data['hotkey_hide'] = (HOOKS.hide_key if HOOKS else '')
                data['hotkey_show'] = (HOOKS.show_key if HOOKS else '')
                data['hotkey_tray'] = bool(HOOKS and HOOKS.used_tray)
                # 全局导入热键触发标记：读取后立即重置，前端轮询到就弹导入框
                if HOOKS and HOOKS.import_requested:
                    data['import_requested'] = True
                    HOOKS.import_requested = False
                else:
                    data['import_requested'] = False
                return self._json(data)
            if path == '/api/browse':
                target = (parse_qs(parsed.query).get('path') or [''])[0]
                return self._json(list_dirs(target))
            if path == '/api/clipboard':
                return self._json({'text': clipboard_text()})
            if path == '/api/pick-dir':
                return self._json(pick_directory())
            if path.startswith('/preview/'):
                name = path[len('/preview/'):]
                target = (DATA_DIR / 'preview' / name).resolve()
                if not str(target).startswith(str((DATA_DIR / 'preview').resolve())):
                    return self._json({'error': 'forbidden'}, 403)
                return self._serve_file(target, 'video/mp4')
            if path.startswith('/api/'):
                return self._json({'error': 'unknown endpoint'}, 404)
            return self._file(path)

        def do_POST(self):
            path = urlparse(self.path).path
            body = self._body()
            try:
                if path == '/api/rooms':
                    text = body.get('text', '')
                    lines = body.get('lines') if isinstance(body.get('lines'), list) else text.splitlines()
                    added, skipped, ignored = engine.add_rooms(lines)
                    return self._json({'ok': True, 'added': added,
                                       'skipped': skipped, 'ignored': ignored})
                if path == '/api/rooms/clear':
                    return self._json({'ok': True, 'removed': engine.clear_rooms()})
                if path == '/api/rooms/remove-matching':
                    pattern = body.get('pattern', '')
                    if not pattern:
                        return self._json({'error': '缺少匹配规则'}, 400)
                    try:
                        re.compile(pattern)
                    except re.error as e:
                        return self._json({'error': f'正则无效: {e}'}, 400)
                    removed = engine.remove_matching(pattern)
                    return self._json({'ok': True, 'removed': removed})
                if path.startswith('/api/rooms/') and path.endswith('/preview'):
                    rid = path.split('/')[3]
                    return self._json(engine.preview(rid))
                if path.startswith('/api/rooms/') and path.endswith('/stream'):
                    rid = path.split('/')[3]
                    return self._json(engine.stream_info(rid))
                if path.startswith('/api/rooms/') and path.endswith('/popout'):
                    rid = path.split('/')[3]
                    return self._json(popout_player(engine, rid, (body or {}).get('auto', '')))
                if path == '/api/popout/close':
                    return self._json(close_popup(body.get('rid', '')))
                if path == '/api/popout/ops':
                    # 调试：直接从 Python 侧操作弹窗（绕开 JS 桥，定位问题用）
                    with _POPUPS_LOCK:
                        api = _POPUPS.get(body.get('rid', ''))
                    if api is None:
                        return self._json({'ok': False, 'error': 'no popup'})
                    op = body.get('op')
                    if op == 'geom':
                        return self._json({'ok': True, 'geom': api.geometry()})
                    if op == 'resize':
                        return self._json({'ok': api.resize(body.get('w', 800), body.get('h', 600)),
                                           'geom': api.geometry()})
                    if op == 'fs':
                        return self._json({'ok': True, 'fs': api.toggle_fullscreen(),
                                           'geom': api.geometry()})
                    return self._json({'ok': False, 'error': 'bad op'})
                if path == '/api/rooms/export':
                    return self._json(engine.export_rooms(body.get('dir', ''), body.get('name', '')))
                if path.startswith('/api/rooms/') and path.endswith('/remux'):
                    rid = path.split('/')[3]
                    return self._json(engine.remux(rid))
                if path.startswith('/api/rooms/') and path.endswith('/open-folder'):
                    rid = path.split('/')[3]
                    info = engine.room_folder_info(rid)
                    if not info:
                        return self._json({'error': 'room not found'}, 404)
                    if os.name == 'nt':
                        os.startfile(info['path'])
                    return self._json({'ok': True, 'path': info['path'],
                                       'exact': info['exact']})
                if path.startswith('/api/rooms/') and path.endswith('/check'):
                    rid = path.split('/')[3]
                    if rid not in engine.rooms:
                        return self._json({'error': 'room not found'}, 404)
                    engine.pool.submit(engine._check, rid)
                    return self._json({'ok': True})
                if path == '/api/engine':
                    action = body.get('action')
                    if action == 'start':
                        engine.start()
                    elif action == 'stop':
                        engine.stop()
                    else:
                        return self._json({'error': 'bad action'}, 400)
                    return self._json({'ok': True, 'running': engine.running})
                if path == '/api/open-url':
                    # 详情页「直播链接」点击：必须走系统浏览器（webview 里点 <a> 会把应用自己导航走）
                    u = str(body.get('url') or '').strip()
                    if not re.match(r'^https?://', u):
                        return self._json({'error': '仅支持 http/https 链接'}, 400)
                    webbrowser.open(u)
                    return self._json({'ok': True})
                if path == '/api/hotkeys/suspend':
                    return self._json(HOOKS.suspend() if HOOKS else
                                      {'ok': False, 'error': '窗口钩子未启用'})
                if path == '/api/hotkeys/resume':
                    return self._json(HOOKS.resume() if HOOKS else
                                      {'ok': False, 'error': '窗口钩子未启用'})
                if path == '/api/window':
                    # 界面上的「缩到托盘 / 立即隐藏」都走这里；隐藏后 HTTP 仍可用
                    action = (body or {}).get('action')
                    if not HOOKS:
                        return self._json({'ok': False, 'error': '窗口钩子未启用'}, 400)
                    if action == 'tray':
                        return self._json({'ok': HOOKS.to_tray()})
                    if action == 'hide':
                        return self._json({'ok': HOOKS.hide()})
                    if action == 'show':
                        return self._json({'ok': HOOKS.show()})
                    return self._json({'ok': False, 'error': 'bad action'}, 400)
                if path == '/api/quit':
                    global CLOSE_CONFIRMED
                    CLOSE_CONFIRMED = True
                    self._json({'ok': True})
                    request_quit()
                    return None
                if path == '/api/config':
                    # 保存位置先落地验证：目录不存在就创建，不可写直接报错，
                    # 免得配置存了个无效路径、等到开播才录不出来。
                    raw = str(body.get('save_dir') or '').strip().strip('"')
                    if raw:
                        p = _norm_dir(raw)     # 全角冒号/尾空格/盘符相对路径统一清洗
                        p = p if p.is_absolute() else (engine.base / p)
                        try:
                            p.mkdir(parents=True, exist_ok=True)
                            probe = p / '.recorder_write_test'
                            probe.write_text('ok', encoding='utf-8')
                            probe.unlink()
                        except Exception as e:
                            return self._json({'ok': False,
                                               'error': f'保存位置不可写：{e}'}, 400)
                    cfg = engine.save_config(body)
                    bind = None
                    if HOOKS and ('hotkey_hide' in body or 'hotkey_show' in body):
                        bind = HOOKS.rebind(cfg.get('hotkey_hide', ''),
                                            cfg.get('hotkey_show', ''))
                    return self._json({'ok': True, 'config': cfg, 'hotkeys': bind,
                                       'save_path': str(engine.save_root())})
                if path == '/api/open-folder':
                    folder = engine.save_root()
                    folder.mkdir(parents=True, exist_ok=True)
                    if os.name == 'nt':
                        os.startfile(str(folder))
                    return self._json({'ok': True, 'path': str(folder)})
                if path == '/api/mkdir':
                    base = Path(os.path.expandvars(os.path.expanduser(
                        str(body.get('path') or '').strip().strip('"'))))
                    name = re.sub(r'[\\/:*?"<>|]', '', str(body.get('name') or '')).strip()
                    if not base.is_dir() or not name:
                        return self._json({'ok': False, 'error': '路径或名称不合法'}, 400)
                    new = base / name
                    try:
                        new.mkdir(parents=True, exist_ok=True)
                    except Exception as e:
                        return self._json({'ok': False, 'error': f'创建失败：{e}'}, 400)
                    return self._json({'ok': True, 'path': str(new)})
                if path.startswith('/api/record/') and path.endswith('/stop'):
                    rid = path.split('/')[3]
                    return self._json({'ok': engine.stop_record(rid)})
                if path.startswith('/api/record/') and path.endswith('/start'):
                    rid = path.split('/')[3]
                    res = engine.start_record(rid)
                    return self._json(res, 200 if res.get('ok') else 400)
                return self._json({'error': 'unknown endpoint'}, 404)
            except Exception as e:
                engine.log('error', f'API {path} 异常: {type(e).__name__}: {e}')
                return self._json({'error': f'{type(e).__name__}: {e}'}, 500)

        def do_PATCH(self):
            path = urlparse(self.path).path
            if path.startswith('/api/rooms/'):
                rid = path.rsplit('/', 1)[-1]
                return self._json({'ok': engine.update_room(rid, self._body())})
            return self._json({'error': 'unknown endpoint'}, 404)

        def do_DELETE(self):
            path = urlparse(self.path).path
            if path.startswith('/api/rooms/'):
                rid = path.rsplit('/', 1)[-1]
                return self._json({'ok': engine.remove_room(rid)})
            return self._json({'error': 'unknown endpoint'}, 404)

    return Handler


def find_running_instance(start: int) -> int | None:
    """探测是否已有本程序在运行；有则返回它的端口，避免重复启动多份录同一房间。

    必须绕开系统代理：开了 Clash 之类的代理后 http_proxy 会指向 127.0.0.1:7890，
    urllib 默认会把 127.0.0.1 的请求也塞给代理，返回 502 → 误判「没有实例」，
    于是启动第二个进程，同一个房间被录两遍。

    性能：不能对每个端口串行发 HTTP 请求等超时 —— 本机（尤其装了代理/VPN 驱动时）
    对未监听端口的拒绝有延迟，12 个端口 × 0.5s ≈ 6 秒，全压在启动路径上。
    改为先用底层 socket 并行探活（一次超时的时间），只对真正连得上的端口做 HTTP 校验。
    """
    import socket
    from concurrent.futures import ThreadPoolExecutor

    def alive(port: int) -> int | None:
        s = socket.socket()
        s.settimeout(0.12)
        try:
            return port if s.connect_ex(('127.0.0.1', port)) == 0 else None
        except OSError:
            return None
        finally:
            try:
                s.close()
            except OSError:
                pass

    ports = list(range(start, start + 16))
    with ThreadPoolExecutor(max_workers=len(ports)) as pool:
        cand = [p for p in pool.map(alive, ports) if p]
    if not cand:
        return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for port in cand:
        try:
            with opener.open(f'http://127.0.0.1:{port}/api/state', timeout=1.5) as r:
                data = json.loads(r.read().decode('utf-8'))
            if isinstance(data, dict) and 'rooms' in data and 'stats' in data:
                return port
        except Exception:
            continue
    return None


def pick_port(start: int) -> tuple[ThreadingHTTPServer, int]:
    for port in range(start, start + 12):
        try:
            return ThreadingHTTPServer(('127.0.0.1', port), make_handler(ENGINE)), port
        except OSError:
            continue
    raise SystemExit('端口 8765-8776 均被占用')


def _arg(flag, default=None):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv and len(sys.argv) > sys.argv.index(flag) + 1 else default


def _edge_candidates() -> list[Path]:
    cands = []
    for env in ('PROGRAMFILES(X86)', 'PROGRAMFILES', 'LOCALAPPDATA'):
        base = os.environ.get(env)
        if not base:
            continue
        cands.append(Path(base) / 'Microsoft/Edge/Application/msedge.exe')
        cands.append(Path(base) / 'Google/Chrome/Application/chrome.exe')
    return [p for p in cands if p.is_file()]


APP_TITLE = 'LiveRecorder Studio'
TRAY = None      # WindowHooks 实例：全局热键 + 托盘兜底
HOOKS = None
CLOSE_CONFIRMED = False   # 界面已确认退出，放行窗口关闭
HTTP_PORT = 0             # 本地服务端口（弹窗播放器要拼 URL 用）
_POPUPS = {}              # rid -> 无边框预览子窗口
_POPUPS_LOCK = threading.Lock()


class _PlayerApi:
    """播放页用的 JS 桥：移动/缩放/全屏/关闭，全部走裸 Win32。

    为什么不用 pywebview 自带的 win.move / win.resize / win.destroy：
    它们被 _shown_call 包着（要等子窗口的 shown 事件，最长干等 20s），而弹窗
    子窗口的 shown 不按时触发时——拖一下窗口，window.move 在 UI 线程上死等，
    整窗直接「未响应」（用户强关过）；resize 也永远失败。所以这里的每个操作
    都直接 SetWindowPos / PostMessage，不碰 pywebview 那层锁。
    """

    def __init__(self, title: str):
        self.win = None
        self.title = title
        self.hwnd = 0
        self._fs = False
        self._saved = None

    # ---------- 基础 ----------
    def _hwnd(self) -> int:
        """只信 create_window 拿到的原生句柄，绝不 FindWindowW 按标题找——
        强关过的窗口会留下 Ghost（幽灵窗口）同名顶着，指令发给它就永久卡死。"""
        if self.hwnd:
            return self.hwnd
        try:
            h = int(self.win.native.Handle)
            if h:
                self.hwnd = h
        except Exception:
            pass
        return self.hwnd

    def _scale(self, hwnd: int) -> float:
        import ctypes
        try:
            dpi = ctypes.windll.user32.GetDpiForWindow(hwnd) or 96
        except Exception:
            dpi = 96
        return dpi / 96.0

    # ---------- 几何（JS 侧一律用逻辑像素） ----------
    def geometry(self):
        """返回 [x, y, w, h]（逻辑像素）；找不到窗口返回 None。"""
        hwnd = self._hwnd()
        if not hwnd:
            return None
        import ctypes
        from ctypes import wintypes
        r = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
        s = self._scale(hwnd)
        return [round(r.left / s), round(r.top / s),
                round((r.right - r.left) / s), round((r.bottom - r.top) / s)]

    def move_to(self, x, y):
        hwnd = self._hwnd()
        if not hwnd:
            return False
        import ctypes
        from ctypes import wintypes
        s = self._scale(hwnd)
        r = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
        ctypes.windll.user32.SetWindowPos(hwnd, 0, int(x * s), int(y * s),
                                          r.right - r.left, r.bottom - r.top, 0x0014)
        return True

    def resize(self, w, h):
        hwnd = self._hwnd()
        if not hwnd:
            return False
        import ctypes
        from ctypes import wintypes
        w = max(360, min(int(w), 2400))
        h = max(240, min(int(h), 2400))
        s = self._scale(hwnd)
        r = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
        ctypes.windll.user32.SetWindowPos(hwnd, 0, r.left, r.top,
                                          int(w * s), int(h * s), 0x0014)
        return True

    # ---------- 全屏：铺满整块屏幕（盖任务栏），Esc/再按还原 ----------
    def toggle_fullscreen(self) -> bool:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        hwnd = self._hwnd()
        if not hwnd:
            return self._fs
        if not self._fs:
            r = wintypes.RECT()
            u.GetWindowRect(hwnd, ctypes.byref(r))
            self._saved = (r.left, r.top, r.right - r.left, r.bottom - r.top)
            hmon = u.MonitorFromWindow(hwnd, 2)      # MONITOR_DEFAULTTONEAREST
            mi = type('MONITORINFO', (ctypes.Structure,), {'_fields_': [
                ('cbSize', wintypes.DWORD), ('rcMonitor', wintypes.RECT),
                ('rcWork', wintypes.RECT), ('dwFlags', wintypes.DWORD)]})()
            mi.cbSize = ctypes.sizeof(mi)
            if u.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                rc = mi.rcMonitor
                u.SetWindowPos(hwnd, 0, rc.left, rc.top,
                               rc.right - rc.left, rc.bottom - rc.top, 0x0040)
            self._fs = True
        else:
            x, y, w, h = self._saved or (80, 80, 960, 540)
            u.SetWindowPos(hwnd, 0, x, y, w, h, 0x0040)
            self._fs = False
        return self._fs

    # ---------- 关闭：投递 WM_CLOSE，绕开被锁住的 win.destroy() ----------
    def close(self) -> bool:
        hwnd = self._hwnd()
        if not hwnd:
            return False
        import ctypes
        ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)   # WM_CLOSE
        return True


def popout_player(engine: Engine, rid: str, auto: str = '') -> dict:
    """开一个无边框、置顶的预览悬浮窗播放直播流（pywebview 子窗口）。

    播放页是独立的 static/player.html（hls.js/flv.js + MSE，与抖音网页播放器同套路），
    拖动/缩放/全屏/关闭全走 _PlayerApi 的裸 Win32（见类注释，不碰 _shown_call）。
    不在 webview 模式（Edge --app / 纯浏览器）时返回 fallback，由界面 window.open 兜底。
    """
    try:
        import webview
    except Exception:
        return {'ok': False, 'fallback': True}
    if not getattr(webview, 'guilib', None) or not HTTP_PORT:
        return {'ok': False, 'fallback': True}

    with _POPUPS_LOCK:
        old = _POPUPS.get(rid)
        if old is not None:
            hwnd = old._hwnd()
            if hwnd:
                import ctypes
                u = ctypes.windll.user32
                u.ShowWindow(hwnd, 9)          # SW_RESTORE
                u.SetForegroundWindow(hwnd)
                return {'ok': True, 'reused': True}
            _POPUPS.pop(rid, None)

    name = '直播预览'
    try:
        with engine.lock:
            r = engine.rooms.get(rid)
        if r is not None:
            name = f'预览 · {(r.alias or r.anchor or rid)}'
    except Exception:
        pass

    url = f'http://127.0.0.1:{HTTP_PORT}/player.html?rid={rid}'
    if auto:
        url += f'&auto={auto}'
    api = _PlayerApi(name)
    try:
        win = webview.create_window(
            name, url, js_api=api,
            width=960, height=540,
            frameless=True, easy_drag=False,   # 拖动走 api.move_to（easy_drag 会触发卡死的 window.move）
            on_top=True,
            shadow=False,                       # 连圆角/投影边框一起去掉：真无边框
            resizable=True, min_size=(360, 240),
            background_color='#000000', confirm_close=False)
    except Exception as e:
        return {'ok': False, 'fallback': True, 'error': str(e)}
    if win is None:
        return {'ok': False, 'fallback': True}
    api.win = win

    def _closed():
        with _POPUPS_LOCK:
            if _POPUPS.get(rid) is api:
                _POPUPS.pop(rid, None)

    win.events.closed += _closed
    with _POPUPS_LOCK:
        _POPUPS[rid] = api
    return {'ok': True}


def close_popup(rid: str) -> dict:
    """关掉指定房间的预览悬浮窗（播放页上的 ✕ 按钮走这里）。

    用 WM_CLOSE 而不是 win.destroy()：后者被 _shown_call 锁住，会白等 20 秒。
    """
    with _POPUPS_LOCK:
        api = _POPUPS.get(rid)
    if api is not None:
        api.close()
    return {'ok': True}


def _tray_quit():
    """从托盘菜单退出：先收尾录制，再直接结束进程。"""
    try:
        if ENGINE:
            ENGINE.halt_and_finalize()
    except Exception:
        pass
    os._exit(0)


def _ask_close(win):
    """窗口关闭前的界面二次确认：让 WebView 自己弹对话框。"""
    global CLOSE_CONFIRMED
    try:
        time.sleep(0.02)          # 只留一点让关闭事件收尾的时间，久了会觉得「点 × 没反应」
        win.evaluate_js('window.onHostClose && window.onHostClose()')
    except Exception as e:
        _quit_trace(f'ask close failed: {type(e).__name__}: {e}')
        # 界面调不通就别把人卡死在关不掉的窗口里：直接放行并收尾
        CLOSE_CONFIRMED = True
        try:
            win.destroy()
        except Exception:
            pass
        try:
            if ENGINE:
                ENGINE.halt_and_finalize()
        except Exception:
            pass
        os._exit(0)


def _enable_context_menu(win):
    """WebView2 的默认右键菜单被 pywebview 跟随 debug 标志关掉了，输入框没法右键粘贴。

    两件事都要做：
    1. AreDefaultContextMenusEnabled 持续 12 秒写 True —— CoreWebView2 初始化完成的
       回调里 pywebview 会写回 False，只设一次会输掉竞态（实测用户仍无右键）；
    2. 放行剪贴板读权限 —— pywebview 不处理 PermissionRequested，默认全拒，
       不放行的话 navigator.clipboard.readText() 必失败，自绘菜单的「粘贴」也没戏。
    开发者工具/浏览器快捷键保持关闭（同一 debug 标志管着，不能直接开 debug）。"""
    def run():
        import time
        for _ in range(24):                 # 0.5s × 24 = 12 秒，足够覆盖初始化回调
            try:
                native = win.native
                def _set():
                    native.webview.CoreWebView2.Settings.AreDefaultContextMenusEnabled = True
                    core = native.webview.CoreWebView2
                    # 权限处理器只挂一次
                    if not getattr(native, '_lrs_perm_hooked', False):
                        def on_perm(sender, args):
                            try:
                                kind = str(args.PermissionKind)
                                # 枚举真实取值是 Default=0 / Allow=1 / Deny=2。
                                # 之前写成 0 和 1：0 是 Default，剪贴板读会退化成
                                # WebView2 原生「允许访问剪贴板？」提示（就是弹窗警告）；
                                # 1 又是 Allow，把其他权限全放行了。两个方向都错。
                                if 'Clipboard' in kind or 'clipboard' in kind:
                                    args.State = 1        # Allow
                                else:
                                    args.State = 2        # Deny
                            except Exception:
                                pass
                        core.PermissionRequested += on_perm
                        native._lrs_perm_hooked = True
                if getattr(native, 'InvokeRequired', False):
                    from System import Func, Type
                    native.Invoke(Func[Type](_set))
                else:
                    _set()
            except Exception:
                pass
            time.sleep(0.5)
    threading.Thread(target=run, daemon=True, name='ctx-menu').start()


def _open_window(url: str) -> bool:
    """用内嵌 WebView2 开一个真正的应用窗口：没有地址栏、没有标签页。

    返回 True 表示窗口正常走完了生命周期（用户关掉了窗口）。
    """
    global TRAY, HOOKS
    try:
        import webview
    except Exception as e:
        _quit_trace(f'webview import failed: {type(e).__name__}: {e}')
        return False
    try:
        # 默认几何 = 用户指定的文件夹窗口位置/大小（逻辑像素：741×1398 @ -6,0）
        # LRS_HIDDEN_MAIN=1 仅供自动化测试：主窗口不出面，弹窗子窗口照常工作
        win = webview.create_window(APP_TITLE, url,
                                    width=741, height=1398, x=-6, y=0,
                                    min_size=(741, 520),
                                    hidden=bool(os.environ.get('LRS_HIDDEN_MAIN')),
                                    background_color='#0B0D10', confirm_close=False)

        def _on_closing():
            """点窗口 × 时先拦下，交给界面自己弹二次确认。

            返回 False 取消本次关闭；界面上点「确认退出」后走 /api/quit 正常收尾。
            注意不能在这里同步调 evaluate_js——关闭事件本身在 GUI 线程里，
            同步等 JS 会互相等死，所以丢到独立线程里发。
            """
            if CLOSE_CONFIRMED:
                return True
            threading.Thread(target=_ask_close, args=(win,), daemon=True,
                             name='ask-close').start()
            return False

        win.events.closing += _on_closing
        _enable_context_menu(win)     # 输入框右键粘贴（pywebview 默认关了）

        # 全局热键隐藏/还原（默认 Alt+F1，被占用会自动换键）
        try:
            import tray as hooks_mod
            HOOKS = hooks_mod.WindowHooks(
                APP_TITLE, on_quit=_tray_quit, tip='LiveRecorder Studio',
                icon_path=ICON,
                hide_hotkey=(ENGINE.cfg.get('hotkey_hide', '') if ENGINE else ''),
                show_hotkey=(ENGINE.cfg.get('hotkey_show', '') if ENGINE else ''),
                import_hotkey=(ENGINE.cfg.get('hotkey_import', 'Alt+1') if ENGINE else ''))
            HOOKS.start()
            TRAY = HOOKS
            print(f'热键: 隐藏 {HOOKS.hide_key or "无"} / 还原 {HOOKS.show_key or "无"} / 导入 {HOOKS.import_key or "无"}')
        except Exception as e:
            _quit_trace(f'hooks failed: {type(e).__name__}: {e}')
        # 禁用 WebView2 GPU 渲染：部分显卡驱动下 GPU 加速导致整个页面黑屏
        os.environ.setdefault('WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS', '--disable-gpu')
        webview.start(gui='edgechromium', icon=str(ICON) if ICON else None)
        if HOOKS:
            HOOKS.stop()
        return True
    except Exception as e:
        _quit_trace(f'window failed: {type(e).__name__}: {e}')
        return False


def _open_app_browser(url: str):
    """次级回退：用 Edge/Chrome 的 --app 模式开无标签页窗口，返回进程句柄。"""
    exes = _edge_candidates()
    if not exes:
        return None
    profile = DATA_DIR / '.window'
    try:
        profile.mkdir(parents=True, exist_ok=True)
        creation = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        return subprocess.Popen(
            [str(exes[0]), f'--app={url}', '--window-size=1320,900',
             f'--user-data-dir={profile}', '--no-first-run',
             '--no-default-browser-check'],
            creationflags=creation)
    except Exception:
        return None


def _redirect_std():
    """打包成无控制台 exe 后 stdout 为 None，print 会直接抛异常。

    把标准输出重定向到 logs/app.log，既避免崩溃，也留下排查痕迹。
    """
    if not getattr(sys, 'frozen', False):
        return
    try:
        logdir = DATA_DIR / 'logs'
        logdir.mkdir(parents=True, exist_ok=True)
        p = logdir / 'app.log'
        if p.is_file() and p.stat().st_size > 256 * 1024:
            p.unlink()      # 防止长期运行把日志堆到几百 MB
        f = open(p, 'a', encoding='utf-8', buffering=1)
        sys.stdout = f
        sys.stderr = f
    except Exception:
        pass


def main():
    global ENGINE, SERVER, HTTP_PORT
    _redirect_std()

    # 已有实例在跑：只把窗口开出来指向它，不再启动第二个（否则会重复录制）
    # 端口基准不能写死 8765：config 里 port 可能被历史漂移污染（如 8792），
    # 写死会检测不到「跑在其他端口的实例」，导致每次都新起进程、热键被反复抢占。
    # find_running_instance 内部扫 8765 起 16 个端口，覆盖默认与漂移范围。
    existing = find_running_instance(8765)
    if existing and '--port' not in sys.argv:
        url = f'http://127.0.0.1:{existing}/'
        print(f'已在运行：{url}')
        if '--no-window' not in sys.argv:
            if _open_window(url):
                return
            _open_app_browser(url)
            return
        if '--no-browser' not in sys.argv:
            webbrowser.open(url)
        return

    ENGINE = Engine()
    # 端口固定从 8765 起步动态分配（或显式 --port），不读 config 里的 port：
    # 那个字段会被历史漂移污染（如 8792），一旦固化，pick_port 起跳点
    # 就与 find_running_instance 的探测范围失配，导致多实例检测失效。
    _port_base = int(_arg('--port') or 8765)
    server, port = pick_port(_port_base)
    SERVER = server
    HTTP_PORT = port
    url = f'http://127.0.0.1:{port}/'
    print('=' * 52)
    print('  LiveRecorder Studio')
    print(f'  界面: {url}')
    print(f'  配置: {ENGINE.config_path}')
    print(f'  录制目录: {ENGINE.save_root()}')
    print('=' * 52)

    # 服务放后台线程，主线程留给窗口：WebView 必须在主线程启动
    th = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.3},
                          daemon=True, name='http')
    th.start()

    def shutdown(code=0):
        try:
            ENGINE.halt_and_finalize()
        except Exception:
            pass
        try:
            server.server_close()
        except Exception:
            pass
        os._exit(code)

    # 1) 主方案：内嵌 WebView2 应用窗口，关窗即退出
    if '--no-window' not in sys.argv:
        if _open_window(url):
            print('窗口已关闭，正在退出')
            shutdown()

        # 2) 回退：Edge/Chrome 的 --app 模式（无标签页），跟随进程生命周期
        proc = _open_app_browser(url)
        if proc:
            print('已用浏览器应用模式打开窗口')
            try:
                while proc.poll() is None and th.is_alive():
                    time.sleep(0.4)
            except KeyboardInterrupt:
                pass
            shutdown()

        # 3) 最后回退：默认浏览器标签页
        if '--no-browser' in sys.argv:
            print('窗口与浏览器均不可用，仅提供本地服务')

    if ENGINE.cfg.get('auto_open_browser', True) and '--no-browser' not in sys.argv:
        threading.Timer(0.9, lambda: webbrowser.open(url)).start()

    try:
        while th.is_alive():
            th.join(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        shutdown()


if __name__ == '__main__':
    main()
