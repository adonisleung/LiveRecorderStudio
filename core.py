# -*- coding: utf-8 -*-
"""LiveRecorder Studio 内核：零第三方依赖。

职责：配置、直播间解析、错峰调度、ffmpeg 录制、状态与日志。
设计要点：单进程 + 有限并发线程池；每个房间独立相位与失败退避；
网络/解析/录制异常一律分类记录，不静默吞掉，也不误报为「风控」。
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

BASE = (Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False)
        else Path(__file__).resolve().parent)
# 数据目录：房间列表 / 配置 / 录制根目录。可用 LRS_HOME 指到别处，
# 自检和多实例共用同一份程序文件时就不会互相踩用户数据。
DATA_DIR = Path(os.environ.get('LRS_HOME') or BASE)
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')

# 抖音 CDN 校验 Referer，缺失会直接 403；快手不校验，但带上更稳
PROVIDER_REFERER = 'https://live.douyin.com/'
KS_REFERER = 'https://live.kuaishou.com/'
XHS_REFERER = 'https://www.xiaohongshu.com/'

DEFAULT_CONFIG = {
    'interval': 180,
    'jitter_ratio': 4,
    'concurrency': 4,
    'save_dir': 'downloads',
    'format': 'ts',
    'quality': '原画',
    'cookie': '',
    'proxy': '',
    'follow_system_proxy': True,   # 未填代理地址时是否跟随系统代理（对齐原项目行为）
    'auto_open_browser': True,
    'port': 8765,
    'segment_minutes': 0,      # 分段录制时长（分钟），0 = 不分段
    'auto_mp4': True,          # 录制结束自动转封装为 mp4
    'keep_ts': False,          # 转换成功后是否保留原始 .ts
    'hotkey_hide': 'Alt+F1',   # 隐藏窗口（不留托盘图标）
    'hotkey_show': 'Alt+F2',   # 还原窗口
}


PLATFORM_DIRS = {'抖音', '快手', '小红书', '直链', '演示', '自定义', '其他'}


# ttwid 兜底 cookie 留空：真正可靠的是按会话自取（见 _dy_cookie）。
# 旧版此处参考过 ihmily/DouyinLiveRecorder 的公开示例值，出于脱敏已移除。
DY_FALLBACK_COOKIE = ''
_DY_JAR = {'cookie': '', 'ts': 0.0}


def _dy_cookie(cfg: dict, force: bool = False) -> str:
    """抖音访问用 cookie：优先用户填的，其次本进程自取的 ttwid（20 分钟有效）。

    匿名请求也带 ttwid 更像个正常浏览器；缺失时被判定为爬虫的概率明显更高。
    """
    user = str(cfg.get('cookie') or '').strip()
    if user:
        return user
    if not force and _DY_JAR['cookie'] and time.time() - _DY_JAR['ts'] < 1200:
        return _DY_JAR['cookie']
    try:
        import http.cookiejar as _cj
        jar = _cj.CookieJar()
        opener = urllib.request.build_opener(
            _proxy_handler(str(cfg.get('proxy') or ''),
                           bool(cfg.get('follow_system_proxy', True))),
            urllib.request.HTTPCookieProcessor(jar))
        opener.open(urllib.request.Request(
            'https://live.douyin.com/', headers={'User-Agent': UA}), timeout=12).read()
        cookie = '; '.join(f'{c.name}={c.value}' for c in jar)
        if cookie:
            _DY_JAR['cookie'], _DY_JAR['ts'] = cookie, time.time()
            return cookie
    except Exception:
        pass
    return _DY_JAR['cookie'] or DY_FALLBACK_COOKIE


def _norm_dir(raw: str) -> Path:
    """把用户手输的目录规整成合法路径（导出/保存位置都用它）。

    实测踩过的坑（用户「指定导出到 G 盘失败」）：
    - 中文输入法把 'G:\\' 打成 'G：\\'（全角冒号）→ 会在当前盘建一个叫「G：」的文件夹；
    - 'G:' 是盘符相对路径（写到 G 盘当前目录而不是 G:\\ 根）；
    - 路径首尾空格直接 WinError 123。统一清洗后再用。
    """
    s = (raw or '').strip().strip('"').strip()
    s = s.replace('：', ':').replace('＼', '\\').replace('/', '\\')
    if re.match(r'^[A-Za-z]:$', s):        # 'G:' → 'G:\'
        s += '\\'
    while len(s) > 3 and s.endswith('\\'):
        s = s[:-1]
    return Path(os.path.expandvars(os.path.expanduser(s)))


def _safe_dir_name(name: str) -> str:
    """把主播名转成可用的文件夹名：去掉 Windows 非法字符与结尾的点/空格。"""
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', '_', str(name or '')).strip()
    s = re.sub(r'\s+', ' ', s).rstrip('. ')
    return s[:40]


def _merge_dir(src: Path, dst: Path) -> int:
    """把 src 目录里的内容搬到 dst（同名文件自动改名），返回搬运数量。"""
    dst.mkdir(parents=True, exist_ok=True)
    n = 0
    for item in list(src.iterdir()):
        target = dst / item.name
        if target.exists():
            stem, dot, ext = item.name.partition('.')
            i = 2
            while target.exists():
                target = dst / f'{stem}({i}){dot}{ext}'
                i += 1
        try:
            shutil.move(str(item), str(target))
            n += 1
        except Exception as e:
            print(f'[warn] 归档 {item.name} 失败: {e}')
    return n


def platform_of(url: str) -> str:
    u = (url or '').lower()
    if u.startswith('demo://'):
        return '演示'
    if 'douyin.com' in u:
        return '抖音'
    if 'kuaishou.com' in u or 'gifshow.com' in u:
        return '快手'
    if 'xiaohongshu.com' in u or 'xhslink.com' in u:
        return '小红书'
    if '.m3u8' in u or '.flv' in u:
        return '直链'
    return '自定义'


def _referer_for(url: str) -> str:
    plat = platform_of(url)
    if plat == '快手':
        return KS_REFERER
    if plat == '抖音':
        return PROVIDER_REFERER
    if plat == '小红书':
        return XHS_REFERER
    return ''


# ---------- 平台级熔断 ----------
# 快手在请求过快时会返回 errorType「请求过快」，此时该 IP 下所有快手房间都会被降级。
# 若每个房间各自重试，只会把限流窗口越打越长（正是漏录的根因），
# 所以按平台共享一个熔断窗口：命中后整平台静默退避，指数放大到上限。
_PLATFORM_BLOCK: dict[str, dict] = {}
_PLATFORM_BLOCK_MAX = 600


# ---------- 平台级自适应节流 ----------
# 抖音/快手对同 IP 的请求频率极敏感：1378 个房间一起抓会直接 444 或返回验证页。
# gap = 同一平台两次请求的最小间隔；命中限流立刻放大，成功后缓慢回落。
_PACE_BASE = {'抖音': 0.35, '快手': 0.7, '小红书': 0.6}
_PACE = {'gap': {}, 'next': {}}
_PACE_LOCK = threading.Lock()


def pace(plat: str):
    """按平台排队：保证同平台两次请求至少间隔 gap 秒。"""
    base = _PACE_BASE.get(plat, 0.2)
    with _PACE_LOCK:
        gap = _PACE['gap'].setdefault(plat, base)
        now = time.time()
        nxt = _PACE['next'].get(plat, 0.0)
        wait = max(0.0, nxt - now)
        _PACE['next'][plat] = max(now, nxt) + gap
    if wait > 0:
        time.sleep(wait)


def pace_slowdown(plat: str):
    """命中限流：把该平台的请求间隔放大（上限 4 秒）。"""
    base = _PACE_BASE.get(plat, 0.2)
    with _PACE_LOCK:
        _PACE['gap'][plat] = min(4.0, _PACE['gap'].get(plat, base) * 1.8)


def pace_speedup(plat: str):
    """正常返回：间隔缓慢回落，别一恢复就又打满。"""
    base = _PACE_BASE.get(plat, 0.2)
    with _PACE_LOCK:
        g = _PACE['gap'].get(plat, base)
        if g > base:
            _PACE['gap'][plat] = max(base, g * 0.96)


def pace_gap(plat: str) -> float:
    with _PACE_LOCK:
        return round(_PACE['gap'].get(plat, _PACE_BASE.get(plat, 0.2)), 2)


def platform_blocked(plat: str) -> float:
    """该平台剩余静默秒数（0 表示可用）。"""
    item = _PLATFORM_BLOCK.get(plat)
    if not item:
        return 0.0
    left = item['until'] - time.time()
    return left if left > 0 else 0.0


def note_rate_limit(plat: str, base: float = 120.0, cap: float | None = None) -> float:
    """记一次限流，返回本次静默秒数。

    关键点：同一个静默窗口内的并发命中不再累加。否则 6 个线程同时吃 444，
    计数会从 1 直接跳到 6，静默时间瞬间顶到 600s 上限，整个平台被冻住。
    """
    cap = cap or _PLATFORM_BLOCK_MAX
    item = _PLATFORM_BLOCK.setdefault(plat, {'until': 0.0, 'hits': 0})
    now = time.time()
    if item['until'] > now:              # 已在静默中：沿用本轮，不升级
        return item['until'] - now
    item['hits'] += 1
    wait = min(cap, base * (2 ** min(item['hits'] - 1, 3)))
    item['until'] = now + wait
    return wait


def clear_rate_limit(plat: str):
    _PLATFORM_BLOCK.pop(plat, None)


def log_ring_append(engine, level, text):
    engine.log(level, text)


@dataclass
class Room:
    id: str
    url: str
    note: str = ''
    quality: str = '原画'
    enabled: bool = True
    status: str = 'idle'          # idle / checking / offline / live / recording / error
    anchor: str = ''
    alias: str = ''               # 用户自定义显示名，优先于解析出的昵称
    title: str = ''
    source: str = ''
    error: str = ''
    last_check: float = 0
    last_live: float = 0          # 最近一次在播时间，用于「一直不播的沉底」
    file: str = ''
    failures: int = 0
    started_at: float = 0
    rec_bytes: int = 0
    rec_checked: float = 0
    rec_seconds: float = 0       # 本次录制已持续秒数，列表「已录制时长」直接用
    offline_hits: int = 0
    viewers: int = 0              # 最近一次检测时的在线人数快照（不落盘）

    @property
    def display_name(self) -> str:
        # 名字只有两个来源：用户手动设的显示名称、平台解析出的主播昵称。
        # 备注(note)只是备注，绝不参与名字显示。
        if self.alias or self.anchor:
            return self.alias or self.anchor
        # 还没解析出昵称前，不要把整条长链接糊在列表和文件名里
        m = re.search(r'/(\d{4,})/?$', self.url)
        if m:
            return platform_of(self.url) + ' ' + m.group(1)[-6:]
        # 快手是 /u/<快手号>、/profile/<id>，用账号名当占位比域名可读得多
        m = re.search(r'/(?:u|profile|fw/live)/([^/?#]+)', self.url)
        if m:
            return urllib.parse.unquote(m.group(1))
        if self.url.startswith('demo://'):
            return self.url[7:] or '演示房间'
        host = urllib.parse.urlparse(self.url).netloc or self.url
        return host[:24]


@dataclass
class RoomInfo:
    anchor: str
    title: str
    is_live: bool
    stream_url: str = ''
    source: str = ''
    viewers: int = 0              # 本次探测到的当时在线人数


class ResolveError(Exception):
    def __init__(self, kind, detail):
        super().__init__(f'{kind}: {detail}')
        self.kind = kind
        self.detail = detail


def system_proxy() -> str:
    """读系统代理（Clash 之类），返回 http://host:port 或空串。"""
    try:
        proxies = urllib.request.getproxies()
    except Exception:
        return ''
    for key in ('https', 'http'):
        v = str(proxies.get(key) or '').strip()
        if v and '://' in v:
            return v
    return ''


def _proxy_handler(proxy: str, follow_system: bool = False):
    """统一的 ProxyHandler：cookie 会话和普通请求共用同一套代理判定。"""
    if proxy:
        if not proxy.startswith(('http://', 'https://', 'socks5://', 'socks4://')):
            proxy = 'http://' + proxy
        return urllib.request.ProxyHandler({'http': proxy, 'https': proxy})
    if follow_system:
        sysp = system_proxy()
        if sysp:
            return urllib.request.ProxyHandler({'http': sysp, 'https': sysp})
    return urllib.request.ProxyHandler({})


def _opener(proxy: str, follow_system: bool = False):
    """proxy 优先；没填且允许跟随系统代理时用系统代理；否则强制直连。

    注意：直连必须显式传 ProxyHandler({})，否则 urllib 会自动读取
    http_proxy 环境变量，把 127.0.0.1 的本地请求也丢给代理。
    """
    return urllib.request.build_opener(_proxy_handler(proxy, follow_system))


def http_get(url: str, cfg: dict, timeout: int = 12, retries: int = 3,
             referer: str | None = None, headers: dict | None = None) -> str:
    """抓取页面，SNI/TLS 握手超时与瞬时网络错误自动重试（抖音限流时很常见）。

    只对「瞬时」错误重试；4xx 直接抛出，避免把 404 当成网络问题反复打。
    """
    headers = {
        'User-Agent': UA,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9',
        'Cache-Control': 'no-cache',
        'Pragma': 'no-cache',
        **(headers or {}),
    }
    ref = referer if referer is not None else _referer_for(url)
    if ref:
        headers['Referer'] = ref
    if cfg.get('cookie'):
        headers['Cookie'] = cfg['cookie']
    opener = _opener(str(cfg.get('proxy') or ''), bool(cfg.get('follow_system_proxy', True)))
    last = None
    for attempt in range(max(1, retries)):
        req = urllib.request.Request(url, headers=headers)
        try:
            with opener.open(req, timeout=timeout) as resp:
                return resp.read().decode('utf-8', errors='replace')
        except urllib.error.HTTPError as e:
            if e.code in (403, 404, 410):
                raise ResolveError(f'HTTP_{e.code}', f'服务端返回 {e.code}')
            last = ResolveError(f'HTTP_{e.code}', f'服务端返回 {e.code}')
        except urllib.error.URLError as e:
            last = ResolveError('NETWORK', f'网络不可达或超时 ({e.reason})')
        except TimeoutError:
            last = ResolveError('TIMEOUT', f'请求超过 {timeout}s')
        except Exception as e:
            last = ResolveError('NETWORK', f'{type(e).__name__}: {e}')
        if attempt + 1 < retries:
            # 1.2s / 2.4s 退避，抖动避免多房间同时重试
            time.sleep(1.2 * (2 ** attempt) + random.uniform(0, 0.6))
    raise last


def _first_url(value) -> str:
    if isinstance(value, str) and value.startswith('http'):
        return value
    if isinstance(value, dict):
        for key in ('ORIGIN', '原画', 'FULL_HD1', 'HD1', 'SD1', 'SD2'):
            if key in value and isinstance(value[key], str):
                return value[key]
        for v in value.values():
            u = _first_url(v)
            if u:
                return u
    if isinstance(value, list):
        for v in value:
            u = _first_url(v)
            if u:
                return u
    return ''


def _extract_stream(data: dict, prefer: str = 'flv') -> str:
    """取流地址。录制用 flv（断流好切段）；网页预览用 hls（浏览器 MSE 好播）。"""
    stream = data.get('stream_url') or {}
    keys = ('flv_pull_url', 'hls_pull_url_map', 'hls_pull_url')
    if prefer == 'hls':
        keys = ('hls_pull_url_map', 'hls_pull_url', 'flv_pull_url')
    for key in keys:
        u = _first_url(stream.get(key))
        if u:
            return u.replace('\\u0026', '&')
    core = stream.get('live_core_sdk_data') or {}
    pull = core.get('pull_data') or {}
    try:
        obj = json.loads(pull.get('stream_data', '{}'))
        origin = obj.get('data', {}).get('origin', {}).get('main', {})
        return origin.get('flv') or origin.get('hls') or ''
    except Exception:
        return ''


def _parse_room_store(html: str) -> dict:
    """从直播间 HTML 中提取 roomStore.roomInfo.room；兼容抖音的转义 JSON。"""
    patterns = [
        r'(\{\\"state\\":.*?)\]\\n"\]\)',
        r'(\{\\"common\\":.*?)\]\\n"\]\)',
        r'"roomStore":(\{.*?\}),"linkmicStore"',
    ]
    cleaned = html
    for pattern in patterns[:2]:
        m = re.search(pattern, html, re.DOTALL)
        if m:
            cleaned = m.group(1).replace('\\', '').replace('u0026', '&')
            break
    m = re.search(r'"roomStore":(.*?),"linkmicStore"', cleaned, re.DOTALL)
    if not m:
        m = re.search(r'"roomStore":(\{.*?\})\s*,\s*"', cleaned, re.DOTALL)
    if not m:
        raise ResolveError('PARSE', '页面中未找到 roomStore（可能触发验证或页面结构变化）')
    chunk = m.group(1)
    chunk = chunk.split(',"has_commerce_goods"')[0]
    # 括号配平，避免贪婪/非贪婪截断导致 JSON 残缺
    depth, end = 0, 0
    for i, ch in enumerate(chunk):
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    chunk = chunk[:end] if end else chunk + '}}}'
    try:
        obj = json.loads(chunk)
    except Exception as e:
        raise ResolveError('PARSE', f'roomStore JSON 解析失败: {e}')
    # 返回整个 roomInfo：room 里只有直播状态与流地址，
    # 主播信息在 roomInfo.anchor / roomInfo.user / room.owner 三处之一。
    info = obj.get('roomInfo')
    if isinstance(info, dict) and info:
        return info
    return obj.get('room', obj) or {}


def clean_anchor_name(name: str, fallback: str = '') -> str:
    """清洗主播名：去掉 emoji、装饰性组合符号、零宽字符，只保留常用文字。

    抖音昵称常见「懒懒ᰔᩚএ᭄」「小明🔥」这类装饰，直接进文件名会变成乱码或非法字符，
    也会让 UI 排版错乱，所以落库前统一清洗。
    """
    if not name:
        return fallback
    text = unicodedata.normalize('NFKC', str(name))
    out = []
    for ch in text:
        code = ord(ch)
        if (0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF       # CJK 汉字
                or 0xF900 <= code <= 0xFAFF                            # 兼容汉字
                or 0x3040 <= code <= 0x30FF                            # 日文假名
                or 0xAC00 <= code <= 0xD7A3                            # 韩文
                or 0x0041 <= code <= 0x005A or 0x0061 <= code <= 0x007A  # 拉丁字母
                or 0x00C0 <= code <= 0x024F                            # 带音标拉丁
                or 0x0030 <= code <= 0x0039):                          # 数字
            out.append(ch)
        elif ch in ' -_.·':
            out.append(ch)
    cleaned = re.sub(r'\s+', ' ', ''.join(out)).strip(' -_.·')
    # 纯数字是房间号 / uid，不是主播昵称。解析拿不到昵称时曾被它兜底，
    # 结果列表里整片显示成一串数字，必须在入口就掐掉。
    if re.fullmatch(r'\d+', cleaned or ''):
        return ''
    return cleaned or fallback or str(name).strip()


def is_junk_anchor(name: str) -> bool:
    """判断「解析失败留下的占位名」：空、纯数字、单字符。"""
    s = (name or '').strip()
    return not s or bool(re.fullmatch(r'\d+', s)) or len(s) <= 1


def _anchor_of(info: dict, html: str, rid: str) -> str:
    """主播昵称可能在 roomInfo.anchor / roomInfo.user / room.owner，三处都要查。"""
    room = info.get('room') if isinstance(info.get('room'), dict) else {}
    candidates = [info.get('anchor'), info.get('user'), info.get('anchor_info'),
                  room.get('owner'), room.get('anchor'), info]
    for obj in candidates:
        if isinstance(obj, dict):
            # 只认 nickname：display_id 是抖音号（'1'/'Y' 这类短串），拿它当主播名
            # 会把好名字覆盖成乱码编号
            v = obj.get('nickname')
            if isinstance(v, str) and v.strip():
                return v.strip()
    # 兜底：HTML 里的 anchor/nickname 是转义形式（\"anchor\":{...}），先反转义再匹配
    clean = html.replace('\\"', '"').replace('\\\\', '\\').replace('\\u0026', '&')
    for pat in (r'"anchor":\{[^}]*?"nickname":"([^"]+)"',
                r'"owner":\{[^}]*?"nickname":"([^"]+)"',
                r'"anchorInfo":\{[^}]*?"nickname":"([^"]+)"',
                r'"nickname":"([^"]{1,40})","avatar_thumb'):
        m = re.search(pat, clean)
        if m:
            name = m.group(1).strip()
            if name:
                return name
    return rid


def _viewers_of(room: dict) -> int:
    """取「当时在线人数」快照：优先 room_view_stats.display_value（数值），
    其次 user_count_str。大房间可能给「1.2万」这种缩写，一并换算。
    拿不到返回 0，前端显示「—」。
    """
    if not isinstance(room, dict):
        return 0
    stats = room.get('room_view_stats')
    detail = room.get('stats')
    candidates = [stats.get('display_value') if isinstance(stats, dict) else None,
                  room.get('user_count_str'),
                  detail.get('user_count_str') if isinstance(detail, dict) else None]
    for v in candidates:
        if v is None or v == '':
            continue
        s = str(v).strip()
        try:
            if '万' in s:
                return max(0, int(float(s.replace('万', '')) * 10000))
            return max(0, int(float(s)))
        except (TypeError, ValueError):
            continue
    return 0


DY_API = ('https://live.douyin.com/webcast/room/web/enter/?aid=6383&app_name=douyin_web'
          '&live_id=1&device_platform=web&language=zh-CN&enter_from=web_live&cookie_enabled=true'
          '&screen_width=1920&screen_height=1080&browser_language=zh-CN&browser_platform=Win32'
          '&browser_name=Chrome&browser_version=131.0.0.0&web_rid={rid}')


def _dy_api_room(rid: str, cfg: dict):
    """抖音 webcast 接口（1~5 KB JSON），拿不到就返回 None 让调用方回退 SSR。

    这条接口比直播间 HTML（约 1 MB）轻两个数量级：同一出口 IP 上，
    HTML 大概 40~50 次就会被丢进「验证码中间页」，而接口还能继续用。
    所以优先走接口，实在不通再退回老路径。
    """
    cookie = _dy_cookie(cfg)
    headers = {'Accept': 'application/json, text/plain, */*'}
    if cookie:
        headers['Cookie'] = cookie
    try:
        txt = http_get(DY_API.format(rid=rid), cfg, timeout=10, retries=1,
                       referer='https://live.douyin.com/', headers=headers)
    except ResolveError as e:
        if e.kind in ('HTTP_444', 'HTTP_403', 'HTTP_429'):
            raise ResolveError('RATE_LIMIT', f'抖音风控拦截（{e.kind}），单 IP 请求过快')
        return None
    if not txt or not txt.lstrip().startswith('{'):
        return None                     # 空响应：接口要签名/被降级，交给 SSR 兜底
    try:
        obj = json.loads(txt)
    except Exception:
        return None
    if obj.get('status_code') not in (0, None):
        return None
    rooms = ((obj.get('data') or {}).get('data') or [])
    return rooms[0] if rooms else None


def _douyin(url: str, cfg: dict, prefer: str = 'flv') -> RoomInfo:
    rid = url.split('?')[0].rstrip('/').split('/')[-1]
    page = f'https://live.douyin.com/{rid}'
    pace('抖音')

    room = _dy_api_room(rid, cfg)
    if room is not None:
        pace_speedup('抖音')
        # 未开播房间接口不给 owner，交给上层保留旧昵称，别用房间号覆盖
        anchor = clean_anchor_name((room.get('owner') or {}).get('nickname') or '', '')
        title = room.get('title') or ''
        stream = _extract_stream(room, prefer)
        live = room.get('status') == 2 or bool(stream)
        viewers = _viewers_of(room) if live else 0
        if live:
            if not stream:
                raise ResolveError('NO_STREAM', '已开播但未解析到流地址（可能仅支持 App 端）')
            return RoomInfo(anchor=anchor, title=title, is_live=True,
                            stream_url=stream, source='douyin', viewers=viewers)
        return RoomInfo(anchor=anchor, title=title, is_live=False,
                        source='douyin', viewers=0)

    cookie = _dy_cookie(cfg)
    try:
        html = http_get(page, cfg, headers={'Cookie': cookie} if cookie else None)
    except ResolveError as e:
        if e.kind in ('HTTP_444', 'HTTP_403', 'HTTP_429'):
            pace_slowdown('抖音')
            raise ResolveError('RATE_LIMIT', f'抖音风控拦截（{e.kind}），单 IP 请求过快')
        raise
    # 风控三级形态，处理方式完全不同：
    #   1) 444/403/429   → 请求过快，退避后自愈
    #   2) 验证码中间页  → 该出口 IP 已被判定为机器，退避几乎无效，必须换 IP
    #   3) 无 roomStore  → 页面结构变化或轻量拦截，换 ttwid 再试
    if '验证码中间页' in html or ('captcha' in html.lower() and len(html) < 8000):
        pace_slowdown('抖音')
        raise ResolveError('BLOCKED',
                           '抖音返回验证码中间页：当前出口 IP 已被风控（换代理/IP 或加大检查间隔）')
    # 真房间页一定带 roomStore；没有就是验证页/风控页，别当成「未开播」
    if 'roomStore' not in html:
        pace_slowdown('抖音')
        _dy_cookie(cfg, force=True)     # 下一轮换新 ttwid 再试
        raise ResolveError('RATE_LIMIT', '抖音返回验证页（没有 roomStore），请求过快')
    pace_speedup('抖音')
    info = _parse_room_store(html)
    room = info.get('room') if isinstance(info.get('room'), dict) else info
    anchor = clean_anchor_name(_anchor_of(info, html, rid), '')
    title = room.get('title') or ''
    status = room.get('status')
    stream = _extract_stream(room, prefer)
    live = status == 2 or bool(stream)
    viewers = _viewers_of(room) if live else 0
    # status: 2=直播中，4=已结束。以「能否取到流地址」为准，
    # 因为 HTML 里的 roomStore 可能是 SSR 缓存，状态会滞后于真实开播。
    if live:
        if not stream:
            raise ResolveError('NO_STREAM', '已开播但未解析到流地址（可能仅支持 App 端）')
        return RoomInfo(anchor=anchor, title=title, is_live=True,
                        stream_url=stream, source='douyin', viewers=viewers)
    return RoomInfo(anchor=anchor, title=title, is_live=False,
                    source='douyin', viewers=0)


KS_UID_RE = re.compile(r'live\.kuaishou\.com/(?:u|profile|fw/live)/([^/?#]+)')


def _ks_did_cookie() -> str:
    """生成快手 did 设备 Cookie：没有它快手一律返回「请求过快」。"""
    import uuid
    return f'did=web_{uuid.uuid4().hex}; didv={int(time.time() * 1000)}'


def _ks_mobile_api(eid: str, cfg: dict) -> dict | None:
    """快手移动端 API（优先方案，比网页抓取更不容易触发风控）。

    POST livev.m.chenzhongtech.com/rest/k/live/byUser
    失败返回 None，由调用方降级到网页抓取。
    """
    api = 'https://livev.m.chenzhongtech.com/rest/k/live/byUser?kpn=GAME_ZONE&captchaToken='
    body = json.dumps({
        'source': 5, 'eid': eid,
        'shareMethod': 'card', 'clientType': 'WEB_OUTSIDE_SHARE_H5',
    }).encode()
    headers = {
        'User-Agent': 'ios/7.830 (ios 17.0; ; iPhone 15 (A2846/A3089/A3090/A3092))',
        'Accept-Language': 'zh-CN,zh;q=0.8',
        'Referer': 'https://www.kuaishou.com/',
        'content-type': 'application/json',
        'Cookie': _ks_did_cookie(),
    }
    if cfg.get('cookie'):
        headers['Cookie'] = cfg['cookie']
    opener = _opener(str(cfg.get('proxy') or ''), bool(cfg.get('follow_system_proxy', True)))
    try:
        req = urllib.request.Request(api, data=body, headers=headers, method='POST')
        with opener.open(req, timeout=12) as resp:
            obj = json.loads(resp.read().decode('utf-8', errors='replace'))
    except Exception:
        return None
    if not isinstance(obj, dict) or obj.get('result') != 1:
        return None
    return obj.get('liveStream') or {}


def _ks_state(html: str) -> dict:
    """提取快手的 window.__INITIAL_STATE__。

    快手这段不是标准 JSON：里面有 `"authToken":undefined` 这类 JS 字面量，
    直接 json.loads 会在 undefined 处炸掉，所以先按字符串/括号规则卡出大括号范围，
    再把裸 undefined 换成 null。
    """
    m = re.search(r'__INITIAL_STATE__\s*=\s*', html)
    if not m:
        raise ResolveError('PARSE', '页面中未找到 __INITIAL_STATE__（可能触发验证或页面结构变化）')
    s = html[m.end():]
    depth = end = 0
    in_str = esc = False
    for i, ch in enumerate(s):
        if esc:
            esc = False
            continue
        if ch == '\\':
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if not end:
        raise ResolveError('PARSE', '快手状态 JSON 未闭合')
    raw = s[:end]
    raw = re.sub(r':\s*undefined\b', ':null', raw)
    raw = re.sub(r':\s*NaN\b', ':null', raw)
    try:
        obj = json.loads(raw)
    except Exception as e:
        raise ResolveError('PARSE', f'快手状态 JSON 解析失败: {e}')
    if not isinstance(obj, dict):
        raise ResolveError('PARSE', '快手状态结构异常')
    return obj


def _ks_pick_stream(live_stream: dict, quality: str) -> str:
    """从 playUrls 里挑一条可直录的流。

    优先 h264（任何播放器和 mp4 转封装都吃），没有再用 hevc；
    画质按 name 精确匹配，匹配不到就按码率档位就近取。
    """
    pools = []
    play_urls = live_stream.get('playUrls') or {}
    for codec in ('h264', 'hevc'):
        node = play_urls.get(codec) or {}
        reps = ((node.get('adaptationSet') or {}).get('representation')) or []
        items = [r for r in reps
                 if isinstance(r, dict) and str(r.get('url', '')).startswith('http')]
        if items:
            pools.append(sorted(items, key=lambda r: r.get('bitrate') or 0, reverse=True))
    if not pools:
        return (live_stream.get('hlsPlayUrl') or live_stream.get('url') or '').strip()
    reps = pools[0]
    target = (quality or '').strip()
    if target in ('', '原画'):
        return reps[0]['url']
    for r in reps:
        if target in (r.get('name'), r.get('shortName')):
            return r['url']
    rank = {'超清': 0, '高清': 1, '标清': 2, '流畅': 3}
    idx = rank.get(target, 0)
    return reps[min(idx, len(reps) - 1)]['url']


def _kuaishou(url: str, cfg: dict) -> RoomInfo:
    m = KS_UID_RE.search(url)
    uid = urllib.parse.unquote(m.group(1)) if m else ''
    page = url.split('?')[0]
    if 'kuaishou.com' not in page:
        if not uid:
            raise ResolveError('UNSUPPORTED', '无法识别快手直播间地址')
        page = f'https://live.kuaishou.com/u/{uid}'
    pace('快手')

    # ── 方案 A：移动端 API（优先，更不容易触发风控） ──
    eid = uid or (KS_UID_RE.search(page).group(1) if KS_UID_RE.search(page) else '')
    if eid:
        ls = _ks_mobile_api(eid, cfg)
        if ls:
            living = bool(ls.get('living'))
            author = (ls.get('user') or {}).get('user_name') or ''
            anchor = clean_anchor_name(author, '')
            if living:
                stream = ''
                pu = ls.get('multiResolutionPlayUrls') or ls.get('playUrls') or []
                if pu and isinstance(pu[0], dict):
                    urls = pu[0].get('urls') or []
                    if urls:
                        stream = urls[0].get('url') if isinstance(urls[0], dict) else str(urls[0])
                if not stream:
                    stream = (ls.get('hlsPlayUrl') or ls.get('url') or '').strip()
                if not stream:
                    raise ResolveError('NO_STREAM', '已开播但未解析到播放地址（可能为付费/私密直播）')
                return RoomInfo(anchor=anchor, title='', is_live=True,
                                stream_url=stream, source='kuaishou')
            return RoomInfo(anchor=anchor, title='', is_live=False, source='kuaishou')

    # ── 方案 B：网页抓取（兜底） ──
    ks_headers = {'Cookie': _ks_did_cookie()}
    if cfg.get('cookie'):
        ks_headers['Cookie'] = cfg['cookie']
    html = http_get(page, cfg, headers=ks_headers)
    state = _ks_state(html)
    liveroom = state.get('liveroom') or {}
    play = liveroom.get('playList') or []
    first = play[0] if play and isinstance(play[0], dict) else {}
    # 快手限流的真实形态：playList[0] 里塞 errorType，liveStream/author 都是空对象。
    # 这不是「没开播」，当成未开播会把正在录的房间误判下播，必须单独识别。
    err = first.get('errorType')
    if isinstance(err, dict) and err:
        msg = f"{err.get('title') or ''}{err.get('content') or ''}".strip() or '请求过快'
        raise ResolveError('RATE_LIMIT', f'快手限流：{msg}')
    live_stream = first.get('liveStream') or {}
    author = first.get('author') or liveroom.get('author') or {}
    if not live_stream and not author:
        raise ResolveError('RATE_LIMIT', '快手返回降级页面（请求过快），已按平台熔断退避')
    name = author.get('name') or ''
    if not name:
        # 未开播时 playList 为空，页头 <title>孙妮-快手直播</title> 是唯一稳定来源
        t = re.search(r'<title[^>]*>(.*?)</title>', html, re.DOTALL)
        if t:
            name = re.sub(r'[-－]\s*快手直播\s*$', '', t.group(1)).strip()
    if name in ('快手直播', '快手'):
        name = ''
    anchor = clean_anchor_name(name, '')
    living = bool(first.get('isLiving'))
    if living:
        stream = _ks_pick_stream(live_stream, cfg.get('quality', '原画'))
        if not stream:
            raise ResolveError('NO_STREAM', '已开播但未解析到播放地址（可能为付费/私密直播）')
        return RoomInfo(anchor=anchor, title='', is_live=True,
                        stream_url=stream, source='kuaishou')
    return RoomInfo(anchor=anchor, title='', is_live=False, source='kuaishou')


XHS_ROOM_RE = re.compile(r'xiaohongshu\.com/(?:livestream|live)/(\d{6,})')
XHS_HEADERS = {
    # 小红书网页版 SSR 只返回空房间（roomId=0），换成 App 的 UA + 通用参数才下发 roomData
    'User-Agent': 'ios/7.830 (ios 17.0; ; iPhone 15 (A2846/A3089/A3090/A3092))',
    'xy-common-params': 'platform=iOS&sid=session.1722166379345546829388',
    'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8',
}


def _xhs_state(html: str) -> dict:
    m = re.search(r'window\.__INITIAL_STATE__=(.*?)</script>', html, re.S)
    if not m:
        raise ResolveError('PARSE', '页面中未找到 __INITIAL_STATE__（可能触发验证）')
    raw = re.sub(r'(?<=[:,\[])\s*undefined\b', ' null', m.group(1))
    try:
        obj = json.loads(raw)
    except Exception as e:
        raise ResolveError('PARSE', f'小红书状态解析失败: {e}')
    if not isinstance(obj, dict):
        raise ResolveError('PARSE', '小红书状态结构异常')
    return obj


def _xhs_pick_stream(pull_config, quality: str) -> str:
    """pullConfig 里的 h264/h265 列表 → 一路可直录的流。"""
    cfg = pull_config
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except Exception:
            cfg = {}
    items = []
    for codec in ('h264', 'h265'):
        for it in (cfg or {}).get(codec) or []:
            u = str((it or {}).get('master_url') or '')
            if u.startswith('http'):
                items.append((u, str((it or {}).get('quality_type_name') or '')))
    if not items:
        return ''
    # 录制优先 flv：mpegts 直拷最稳；退回 m3u8
    flv = [x for x in items if '.flv' in x[0].lower()]
    pool = flv or items
    q = (quality or '').strip()
    if q and q != '原画':
        for u, name in pool:
            if q in name:
                return u
    return pool[0][0]


def _xhs(url: str, cfg: dict) -> RoomInfo:
    m = XHS_ROOM_RE.search(url)
    if not m:
        raise ResolveError('UNSUPPORTED', '无法识别小红书直播间地址')
    rid = m.group(1)
    page = f'https://www.xiaohongshu.com/livestream/{rid}'
    pace('小红书')
    html = http_get(page, cfg, timeout=15, retries=3,
                    referer='https://app.xhs.cn/', headers=XHS_HEADERS)
    state = _xhs_state(html)
    ls = state.get('liveStream') or {}
    room = ((ls.get('roomData') or {}).get('roomInfo') or {})
    title = room.get('roomTitle') or ''
    anchor = ''
    deeplink = room.get('deeplink') or ''
    if deeplink:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(deeplink).query)
        anchor = (q.get('host_nickname') or [''])[0]
    if not room.get('roomId'):
        # 连房间号都没有：不是「没开播」，而是这次没拿到数据（登录态/风控/网络）
        raise ResolveError('NO_DATA', '小红书返回空房间数据，可能需要登录 Cookie 或稍后重试')
    if ls.get('liveStatus') != 'success':
        return RoomInfo(anchor=clean_anchor_name(anchor, ''), title=title,
                        is_live=False, source='xiaohongshu')
    stream = _xhs_pick_stream(room.get('pullConfig'), cfg.get('quality', '原画'))
    if not stream:
        raise ResolveError('NO_STREAM', '已开播但未解析到播放地址（可能为付费/私密直播）')
    return RoomInfo(anchor=clean_anchor_name(anchor, ''), title=title,
                    is_live=True, stream_url=stream, source='xiaohongshu')


def resolve(url: str, cfg: dict, prefer: str = 'flv') -> RoomInfo:
    url = url.strip()
    if url.startswith('demo://'):
        name = url[7:].strip() or '演示房间'
        return RoomInfo(anchor=name, title='本地演示信号', is_live=True,
                        stream_url='demo://testsrc', source='demo')
    if url.endswith('.m3u8') or '.m3u8?' in url or url.endswith('.flv') or '.flv?' in url:
        return RoomInfo(anchor=urllib.parse.urlparse(url).netloc or '自定义源',
                        title='自定义流', is_live=True, stream_url=url, source='custom')
    if 'douyin.com' in url:
        return _douyin(url, cfg, prefer)
    if 'kuaishou.com' in url or 'gifshow.com' in url:
        return _kuaishou(url, cfg)
    if 'xiaohongshu.com' in url or 'xhslink.com' in url:
        return _xhs(url, cfg)
    raise ResolveError('UNSUPPORTED', f'暂不支持该地址：{url}')


# 导入时只认地址本身。别人的导出文件常见「URL,主播名」「画质,URL,备注」
# 「时间,URL」这类格式，一律只抓 URL：主播名靠平台解析，画质用全局默认，
# 备注只由用户在详情里手填 —— 不让导入文件往这两个字段里塞东西。
_IMPORT_URL_RE = re.compile(r'(?:https?|demo)://[^\s,，;；"\'<>|]+', re.I)


def room_id(url: str) -> str:
    return hashlib.sha1(url.strip().encode('utf-8')).hexdigest()[:12]


class Engine:
    def __init__(self, config_path: Path | None = None):
        self.base = DATA_DIR
        self.config_path = config_path or (self.base / 'config.json')
        self.cfg = self._load_config()
        self.lock = threading.RLock()
        self.rooms: OrderedDict[str, Room] = OrderedDict()
        self.logs = deque(maxlen=400)
        self.running = False
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.next_check: dict[str, float] = {}
        # 刚导入的房间优先检查/录制：大列表下不能让新房间排到 1000 名以后
        self._fresh: dict[str, float] = {}
        # 正在检查中的房间：限流时一次只有 1 个请求/几秒，绝不能把上千个房间
        # 提前标成「检测中」塞进线程池队列，否则队列堆积、界面全是检测中。
        self._inflight: set[str] = set()
        self._seg_start: dict[str, float] = {}   # 当前分段开始时间
        self._last_stream: dict[str, str] = {}   # 分段续录复用的流地址
        self.processes: dict[str, subprocess.Popen] = {}
        self.err_paths: dict[str, Path] = {}
        self.pool = ThreadPoolExecutor(max_workers=max(2, self.cfg['concurrency']),
                                       thread_name_prefix='probe')
        self._load_rooms()
        # 目录归档放后台线程：上千房间逐个探测旧目录会拖慢启动好几秒，
        # 先把窗口亮出来，归档悄悄做完即可（失败只记日志）
        threading.Thread(target=self._migrate_bg, daemon=True, name='migrate').start()
        self.log('info', '内核就绪，未启动监测')

    def _migrate_bg(self):
        try:
            self.migrate_folders()          # 旧版平铺目录 → 平台子目录
        except Exception as e:
            self.log('warn', f'目录归档跳过：{type(e).__name__}: {e}')

    # ---------- 持久化 ----------
    def _load_config(self) -> dict:
        cfg = dict(DEFAULT_CONFIG)
        try:
            if self.config_path.is_file():
                cfg.update(json.loads(self.config_path.read_text(encoding='utf-8')))
        except Exception as e:
            print(f'[warn] 配置读取失败，使用默认值: {e}')
        return cfg

    def save_config(self, patch: dict) -> dict:
        with self.lock:
            for k, v in patch.items():
                # port 是运行时动态分配的监听端口，绝不能被前端配置持久化：
                # 一旦写进 config，下次启动 pick_port 会从这个被污染的端口起跳，
                # 与 find_running_instance 的基准失配，导致多实例检测失效、热键被抢。
                if k == 'port':
                    continue
                if k in DEFAULT_CONFIG:
                    self.cfg[k] = v
            self.config_path.write_text(
                json.dumps(self.cfg, ensure_ascii=False, indent=2), encoding='utf-8')
            return dict(self.cfg)

    def rooms_path(self) -> Path:
        return self.base / 'rooms.json'

    def _load_rooms(self):
        try:
            if self.rooms_path().is_file():
                data = json.loads(self.rooms_path().read_text(encoding='utf-8'))
                migrated = False
                for item in data:
                    r = Room(**{k: v for k, v in item.items() if k in Room.__dataclass_fields__})
                    r.status = 'idle'
                    r.file = ''
                    r.started_at = 0
                    # 名字只来自 alias(手改) / anchor(抖音解析)，备注只作个人记忆。
                    # 早期为回滚「备注被搬成显示名」临时加的两条自动互填已撤销：
                    # 它们分不清「历史误改」和「正常新房间」——前者会把主播名写进
                    # 空备注（每次启动都写），后者会把只填了显示名的房间名字搬走。
                    # 旧版解析失败时用房间号冒充昵称，清掉，别再显示一串数字
                    if is_junk_anchor(r.anchor):
                        if r.anchor:
                            r.anchor = ''
                            migrated = True
                    self.rooms[r.id] = r
                if migrated:
                    self._save_rooms()
                    self.log('info', '已归位历史字段：备注、显示名称与无效昵称')
        except Exception as e:
            self.log('error', f'房间列表读取失败: {e}')

    def _save_rooms(self):
        try:
            data = [{k: getattr(r, k) for k in
                     ('id', 'url', 'note', 'quality', 'enabled', 'alias', 'anchor')}
                    for r in self.rooms.values()]
            self.rooms_path().write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception as e:
            self.log('error', f'房间列表保存失败: {e}')

    # ---------- 日志 ----------
    def log(self, level: str, text: str):
        with self.lock:
            self.logs.append({'t': datetime.now().strftime('%H:%M:%S'),
                              'level': level, 'text': str(text)[:400]})

    # ---------- 房间管理 ----------
    def add_rooms(self, lines: list[str]) -> tuple[int, int, int]:
        """只从每行里抓 URL，其他内容一概不取。

        返回值 (新增, 重复, 无地址行)。导入文件里的备注/主播名/画质
        全部丢弃：名字靠平台解析，备注只由用户在详情里手填。
        """
        added = skipped = ignored = 0
        with self.lock:
            for raw in lines:
                line = raw.strip()
                if not line or line.startswith('#'):
                    continue
                m = _IMPORT_URL_RE.search(line)
                if not m:
                    ignored += 1          # 整行没有地址，忽略
                    continue
                url = m.group(0).rstrip('.,;:!?）)】]》>')
                rid = room_id(url)
                if rid in self.rooms:
                    skipped += 1
                    continue
                self.rooms[rid] = Room(id=rid, url=url, quality=self.cfg['quality'])
                # 新加入的房间立刻检查：用户刚粘贴就想看到结果，
                # 真正的削峰由并发上限 + 调度器抖动负责，而不是让人干等。
                self.next_check[rid] = 0
                self._fresh[rid] = time.time()
                added += 1
            self._save_rooms()
        tail = f'，忽略无地址 {ignored} 行' if ignored else ''
        self.log('info', f'导入完成：新增 {added}，跳过重复 {skipped}{tail}')
        return added, skipped, ignored

    def update_room(self, rid: str, patch: dict) -> bool:
        with self.lock:
            r = self.rooms.get(rid)
            if not r:
                return False
            for k in ('note', 'quality', 'enabled', 'alias'):
                if k in patch:
                    v = patch[k]
                    setattr(r, k, v.strip() if isinstance(v, str) else v)
            if not r.enabled:
                self._stop_record(rid)
                r.status = 'idle'
            self._save_rooms()
            return True

    def remove_room(self, rid: str) -> bool:
        with self.lock:
            self._stop_record(rid)
            ok = self.rooms.pop(rid, None) is not None
            self.next_check.pop(rid, None)
            self._save_rooms()
            return ok

    def clear_rooms(self) -> int:
        with self.lock:
            for rid in list(self.rooms):
                self._stop_record(rid)
            n = len(self.rooms)
            self.rooms.clear()
            self.next_check.clear()
            self._save_rooms()
            return n

    def remove_matching(self, pattern: str) -> int:
        """按主播显示名正则匹配批量移除（不会删已录制文件）。"""
        with self.lock:
            rex = re.compile(pattern)
            targets = [rid for rid, r in self.rooms.items() if rex.search(r.display_name)]
            for rid in targets:
                self._stop_record(rid)
                self.rooms.pop(rid, None)
                self.next_check.pop(rid, None)
            if targets:
                self._save_rooms()
            return len(targets)

    # ---------- 引擎 ----------
    def start(self):
        with self.lock:
            if self.running:
                return
            self.running = True
            self.stop_event.clear()
            # 首轮必须在十几秒内铺开：按 interval 随机会让用户干等几分钟，
            # 误以为「加了房间没反应」。并发上限天然削峰，不需要靠等待。
            spread = min(12.0, max(2.0, float(self.cfg['interval'])))
            now = time.time()
            for rid, r in self.rooms.items():
                if not r.enabled or rid in self.processes:
                    continue
                # 只在「没有排期」或「排期过远」时重排，避免打断已有的错峰相位
                if rid not in self.next_check or self.next_check[rid] - now > spread:
                    self.next_check[rid] = now + random.uniform(0, spread)
            self.thread = threading.Thread(target=self._loop, daemon=True, name='scheduler')
            self.thread.start()
        self.log('ok', f"监测已启动：{len(self.rooms)} 个房间，间隔 {self.cfg['interval']}s，"
                       f"并发 {self.cfg['concurrency']}")

    def halt_and_finalize(self, budget: float = 4.0):
        """退出程序专用：快速终止录制并尽力转码，总耗时有上限。

        不能用 stop() 那条路径——它逐个 wait(timeout=3) 再 join，
        多房间时会把「退出」拖到十几秒，用户会以为程序卡死。
        """
        self.running = False
        self.stop_event.set()
        items = list(self.processes.items())
        for _, proc in items:
            try:
                proc.terminate()          # 先温和结束，让 ffmpeg 写完文件尾
            except Exception:
                pass
        deadline = time.time() + 1.5
        for _, proc in items:
            try:
                proc.wait(timeout=max(0.2, deadline - time.time()))
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self.processes.clear()
        # 尽力转 mp4，超出预算就放弃（保留 ts，下次可手动转）
        for rid, _ in items:
            if time.time() > deadline + budget:
                self.log('warn', '退出时限已到，剩余文件保留为 ts')
                break
            with self.lock:
                r = self.rooms.get(rid)
                rel = r.file if r else ''
            if rel:
                try:
                    self._remux(rid, rel)
                except Exception:
                    pass

    def stop(self):
        with self.lock:
            self.running = False
            self.stop_event.set()
            rids = list(self.processes.keys())
        # 并行收尾：串行等待会让「退出程序」在多房间同时录制时卡十几秒
        if rids:
            threads = [threading.Thread(target=self._stop_record, args=(rid,), daemon=True)
                       for rid in rids]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)
        self.log('warn', '监测已停止')

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                self._watch_processes()
                now = time.time()
                due = []
                with self.lock:
                    for rid, r in self.rooms.items():
                        if not r.enabled:
                            continue
                        # 平台熔断期内整批跳过：继续排期只会把限流窗口越拉越长
                        if platform_blocked(platform_of(r.url)):
                            continue
                        # 正在录制的房间不再扫描：主播下播由 _watch_processes 的
                        # ffmpeg 进程监控兜底（进程退出即收尾），重复扫描同一正在
                        # 录制的直播间纯属浪费资源、徒增风控风险。
                        if rid in self.processes:
                            continue
                        if self.next_check.get(rid, 0) <= now:
                            due.append(rid)
                # 新加的房间插队；其余按「扫描优先级」排序：
                #   分数 = 逾期时长 + 最近开播加成 —— 最近开播/正在播的主播优先检测
                #   （刚播过的更可能马上再播），同时逾期分随时间线性增长，
                #   保证一直没播的房间最终也能轮到（不会被饿死）。
                interval = max(5, self.cfg['interval'])

                def _prio(rid: str):
                    r = self.rooms[rid]
                    due_at = self.next_check.get(rid, 0.0)
                    overdue = now - due_at
                    age = now - (r.last_live or 0.0)
                    if r.status in ('recording', 'live'):
                        bonus = 8.0 * interval        # 在播/录制中：高频复检以及时发现下播
                    elif age < 1800:
                        bonus = 5.0 * interval        # 半小时内播过
                    elif age < 7200:
                        bonus = 3.0 * interval        # 2 小时内播过
                    elif age < 21600:
                        bonus = 1.5 * interval        # 6 小时内播过
                    else:
                        bonus = 0.0
                    return (0 if rid in self._fresh else 1, -(overdue + bonus), due_at)

                due.sort(key=_prio)
                with self.lock:
                    budget = max(1, int(self.cfg['concurrency'])) - len(self._inflight)
                for rid in due:
                    if budget <= 0:
                        break
                    with self.lock:
                        if rid in self._inflight:
                            continue
                        r = self.rooms.get(rid)
                        if not r:
                            continue
                        r.status = 'checking'
                        interval = max(5, self.cfg['interval'])
                        ratio = max(0, int(self.cfg.get('jitter_ratio') or 0))
                        jitter = interval // ratio if ratio else 5
                        self.next_check[rid] = now + interval + random.randint(-jitter, jitter)
                        self._inflight.add(rid)
                    self.pool.submit(self._run_check, rid)
                    budget -= 1
                self.stop_event.wait(0.4)
            except Exception as e:
                self.log('error', f'调度循环异常: {type(e).__name__}: {e}')
                self.stop_event.wait(1)

    def _watch_processes(self):
        """监控录制进程：退出即记录原因并安排重试；僵死（无增长）则重启。

        这是「UI 显示录制中、实际早就断了」的根因——原实现只跳过录制中的房间，
        从不检查 ffmpeg 是否还活着。
        """
        now = time.time()
        dead, stalled, rotate = [], [], []
        with self.lock:
            for rid, proc in list(self.processes.items()):
                r = self.rooms.get(rid)
                if proc.poll() is not None:
                    dead.append((rid, proc.returncode))
                    continue
                if not r:
                    continue
                size = self._file_size(r)
                # 进程还活着就是录制中：调度器把状态改成 checking 后若遇到限流等异常，
                # 状态会卡在「检测中」，这里按事实纠正回来。
                if r.status != 'recording':
                    r.status = 'recording'
                # 时长以调度线程的秒级心跳为准：ffmpeg 挂了也能停在上次已知值
                if r.started_at:
                    r.rec_seconds = max(r.rec_seconds, now - r.started_at)
                if size != r.rec_bytes:
                    r.rec_bytes = size
                    r.rec_checked = now
                    r.failures = 0          # 文件在增长 = 录制正常，清空退避计数
                elif r.rec_checked and now - r.rec_checked > 120 and size > 0:
                    # 2 分钟没有任何增长：流已僵死，主动重启
                    stalled.append((rid, size))
                seg = float(self.cfg.get('segment_minutes') or 0)
                if seg > 0 and r.started_at and now - self._seg_start.get(rid, 0) >= seg * 60:
                    rotate.append(rid)
        for rid, code in dead:
            err = self._ffmpeg_err(rid)
            with self.lock:
                self.processes.pop(rid, None)
                r = self.rooms.get(rid)
                if not r:
                    continue
                size = self._file_size(r)
                r.rec_bytes = size
                if r.started_at:
                    r.rec_seconds = max(r.rec_seconds, now - r.started_at)
                if code == 0 and size > 0:
                    # 正常收尾（主播下播或 demo 流结束）：不是故障，不重试、不计失败
                    r.status = 'live'
                    r.error = ''
                    name = r.anchor or r.url[:32]
                    self.log('info', f'[{name}] 录制已正常结束（{size / 1048576:.1f} MB）')
                    self._schedule_remux(rid)
                    continue
                r.failures += 1
                r.status = 'error'
                r.error = f'FFMPEG_EXIT({code}) · {err[:180] or "ffmpeg 未输出原因"}'
                # 退避要够长：抖音 CDN 对短时间内反复取流会直接 403，
                # 5 秒一重试只会把自己打进黑名单。15/30/60/120 秒逐级放大。
                wait = min(120, 15 * (2 ** min(r.failures - 1, 3)))
                self.next_check[rid] = now + wait + random.uniform(0, 5)
                name, anchor = r.anchor or r.url[:32], r.anchor
            self.log('error', f'[{name}] 录制中断（退出码 {code}，已录 '
                              f'{size / 1048576:.1f} MB）：{err[:200]}')
            if anchor:
                self.log('info', f'[{anchor}] 将在 {wait}s 后重新解析流地址并续录')
            self._schedule_remux(rid)
        for rid in rotate:
            with self.lock:
                r = self.rooms.get(rid)
                stream = self._last_stream.get(rid, '')
                if not r or rid not in self.processes or not stream:
                    continue
                self._stop_record(rid)          # 收尾当前分段（会触发转 MP4）
                r.status = 'live'
            self.log('info', f'[{r.anchor}] 分段到时（{self.cfg.get("segment_minutes")} 分钟），'
                             f'开始下一段')
            with self.lock:
                r = self.rooms.get(rid)
                if r:
                    self._start_record(r, stream)

        for rid, size in stalled:
            with self.lock:
                proc = self.processes.pop(rid, None)
                r = self.rooms.get(rid)
                if proc and proc.poll() is None:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                if not r:
                    continue
                r.failures += 1
                r.status = 'error'
                r.error = f'STALLED · 文件 {size / 1048576:.2f} MB 超过 2 分钟无增长'
                self.next_check[rid] = now + 15 + random.uniform(0, 5)
                name = r.anchor or r.url[:32]
            self._schedule_remux(rid)
            self.log('warn', f'[{name}] 录制停滞，已重启录制进程')

    def _rel(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.base.resolve()))
        except ValueError:
            return str(path)

    def _abs(self, rel: str) -> Path:
        """录制文件路径：保存位置允许绝对路径（别的盘），所以两种都要认。"""
        p = Path(rel)
        return p if p.is_absolute() else (self.base / p)

    def _file_size(self, r: Room) -> int:
        try:
            p = self._abs(r.file) if r.file else None
            return p.stat().st_size if p and p.is_file() else 0
        except Exception:
            return 0

    def _ffmpeg_err(self, rid: str) -> str:
        p = self.err_paths.get(rid)
        try:
            if p and p.is_file():
                text = p.read_bytes()[-4000:].decode('utf-8', errors='replace')
                lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
                return ' / '.join(lines[-3:])
        except Exception:
            pass
        return ''

    def _run_check(self, rid: str):
        try:
            self._check(rid)
        except Exception as e:
            self._on_error(rid, 'UNEXPECTED', f'{type(e).__name__}: {e}')
        finally:
            with self.lock:
                self._inflight.discard(rid)

    def _check(self, rid: str):
        with self.lock:
            r = self.rooms.get(rid)
            if not r or not r.enabled:
                return
            cfg = dict(self.cfg)
            url = r.url
        try:
            info = resolve(url, cfg)
            with self.lock:
                r = self.rooms.get(rid)
                if not r:
                    return
                r.last_check = time.time()
                # webcast 接口对「未开播」的房间不返回 owner，anchor 会是空串：
                # 这时要保留上一次解析到的昵称，否则列表里的主播名会退化成房间号。
                renamed = bool(info.anchor) and info.anchor != r.anchor
                if info.anchor:
                    r.anchor = info.anchor
                if info.title:
                    r.title = info.title
                r.source = info.source or r.source
                r.viewers = info.viewers   # 每次检测刷新「当时在线人数」快照
                r.error = ''
                self._fresh.pop(rid, None)      # 已确认过一次，恢复正常排队
                if renamed:
                    self._save_rooms()   # 昵称落盘，下次启动才能按平台归档旧目录
                clear_rate_limit(platform_of(r.url))   # 能正常解析说明限流已解除
                # 注意：解析成功不代表录制成功（403 就发生在解析之后）。
                # failures 只在「确认未开播」或「录制有实际进展」时清零，
                # 否则每次 403 都从 15 秒重新退避，永远升不上去。
                if not info.is_live:
                    r.failures = 0
                if not info.is_live:
                    r.offline_hits += 1
                    # 录制中判定下播要连续两次确认：抖音页面存在缓存，
                    # 单次 offline 可能是脏数据，误停会把正在录的内容掐断。
                    if rid in self.processes and r.offline_hits < 2:
                        self.log('warn', f'[{r.anchor}] 疑似已下播，下轮确认后再收尾')
                    elif rid in self.processes:
                        self._stop_record(rid)
                        r.status = 'offline'
                        self.log('info', f'[{r.anchor}] 直播已结束，录制已保存')
                    else:
                        r.status = 'offline'
                elif rid in self.processes:
                    r.offline_hits = 0
                    r.last_live = time.time()
                    r.status = 'recording'
                else:
                    r.offline_hits = 0
                    r.last_live = time.time()
                    r.status = 'live'
                    self._start_record(r, info.stream_url)
        except ResolveError as e:
            self._on_error(rid, e.kind, e.detail)
        except Exception as e:
            self._on_error(rid, 'UNEXPECTED', f'{type(e).__name__}: {e}')

    def _on_error(self, rid: str, kind: str, detail: str):
        wait = 0.0
        backoff = 0
        name = plat = ''
        with self.lock:
            r = self.rooms.get(rid)
            if not r:
                return
            r.last_check = time.time()
            r.error = f'{kind} · {detail}'
            name, plat = r.anchor or r.url[:40], platform_of(r.url)
            if kind in ('RATE_LIMIT', 'BLOCKED'):
                # 限流/风控不是「这个房间出问题」，而是整平台在当前 IP 上被限制：
                # 不改录制中状态（ffmpeg 还在写盘），只把该平台整体静默一段时间。
                # BLOCKED（验证码中间页）是按 IP 判定的，退避要更狠：10 分钟起，最长 30 分钟。
                if kind == 'BLOCKED':
                    wait = note_rate_limit(plat, base=600, cap=1800)
                else:
                    wait = note_rate_limit(plat)
                pace_slowdown(plat)
                if rid in self.processes:
                    r.status = 'recording'    # 进程还在写盘，别让「限流」把它标成异常
                else:
                    r.status = 'error'
                self.next_check[rid] = time.time() + wait + random.uniform(0, 5)
            else:
                r.status = 'error'
                r.failures += 1
                interval = max(5, self.cfg['interval'])
                backoff = min(120, 10 * (2 ** min(r.failures - 1, 4)))
                self.next_check[rid] = time.time() + interval + backoff
        if kind == 'BLOCKED':
            self.log('warn', f'[{name}] {detail}；{plat} 静默 {wait:.0f}s'
                             f'（这是出口 IP 级别的判定，切换代理节点比单纯等待更快恢复）')
        elif kind == 'RATE_LIMIT':
            self.log('warn', f'[{name}] {detail}；{plat} 平台静默 {wait:.0f}s 后再试'
                             f'（录制中的房间不受影响）')
        else:
            self.log('error', f'[{name}] {kind} · {detail}；下次检查退避 {backoff}s')

    # ---------- 录制 ----------
    def save_root(self) -> Path:
        """录制根目录：支持绝对路径（任意盘符/网络路径）与相对程序目录两种写法。"""
        raw = str(self.cfg.get('save_dir') or 'downloads').strip().strip('"')
        raw = os.path.expandvars(os.path.expanduser(raw))
        p = Path(raw)
        return p if p.is_absolute() else (self.base / p)

    def _room_dir(self, r: Room) -> Path:
        """录制目录：<保存位置>/<平台>/<主播名>，同一主播的不同平台互不混淆。"""
        safe = _safe_dir_name(r.display_name or r.id)
        return self.save_root() / (platform_of(r.url) or '其他') / safe

    def _adopt_legacy(self, r: Room) -> None:
        """认领旧版平铺目录：<主播名>/ → <平台>/<主播名>/。

        旧版把文件直接放在保存根目录下。昵称只有解析出来才知道，所以这里在
        「真正要写文件 / 打开文件夹」时按已知别名逐个试，命中才搬；目标已存在
        同名文件就改名保存，绝不覆盖。
        """
        root = self.save_root()
        if not root.is_dir():
            return
        plat = platform_of(r.url) or '其他'
        names = []
        for n in (r.anchor, r.alias, r.display_name, r.note):
            s = _safe_dir_name(n or '')
            if s and s not in PLATFORM_DIRS and s not in names:
                names.append(s)
        for name in names:
            src = root / name
            if not src.is_dir():
                continue
            dst = root / plat / name
            if dst.resolve() == src.resolve():
                continue
            n_moved = _merge_dir(src, dst)
            try:
                src.rmdir()
            except OSError:
                pass            # 还有搬不动的文件，目录留着
            if n_moved:
                self.log('info', f'[{name}] 旧文件已归档到「{plat}/{name}」（{n_moved} 个）')

    def _record_path(self, r: Room) -> Path:
        self._adopt_legacy(r)
        folder = self._room_dir(r)
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        safe = _safe_dir_name(r.display_name or r.id)
        ext = str(self.cfg.get('format') or 'ts')
        path = folder / f'{safe}_{stamp}.{ext}'
        # 分段轮换常在 1 秒内发生，文件名必须唯一，否则会把上一段覆盖掉
        i = 2
        while path.exists():
            path = folder / f'{safe}_{stamp}_{i}.{ext}'
            i += 1
        return path

    def room_folder(self, rid: str) -> Path | None:
        """该主播的录制目录。还没录过就不建空文件夹，退到平台目录或保存根目录。"""
        info = self.room_folder_info(rid)
        return Path(info['path']) if info else None

    def room_folder_info(self, rid: str) -> dict | None:
        """返回 {path, exact}：exact=False 表示该主播还没有任何录制文件。"""
        with self.lock:
            r = self.rooms.get(rid)
            if not r:
                return None
            self._adopt_legacy(r)
            room_dir = self._room_dir(r)
        if room_dir.is_dir() and any(room_dir.iterdir()):
            return {'path': str(room_dir), 'exact': True}
        if room_dir.is_dir():
            return {'path': str(room_dir), 'exact': True}
        plat_dir = room_dir.parent
        if plat_dir.is_dir():
            return {'path': str(plat_dir), 'exact': False}
        root = self.save_root()
        root.mkdir(parents=True, exist_ok=True)
        return {'path': str(root), 'exact': False}

    def migrate_folders(self) -> list[str]:
        """启动时先扫一遍：已知昵称的房间，其旧版平铺目录直接归档到平台下。"""
        with self.lock:
            rooms = list(self.rooms.values())
        moved = []
        for r in rooms:
            before = len(self.logs)
            self._adopt_legacy(r)
            if len(self.logs) > before:
                moved.append(_safe_dir_name(r.display_name or r.id))
        return moved

    def preview(self, rid: str, seconds: int = 4) -> dict:
        """抓一小段流转成 mp4 供界面预览（浏览器不能直接播 flv）。

        只生成 3–5 秒的低分辨率片段，几十 KB，不会占用明显带宽。
        """
        with self.lock:
            r = self.rooms.get(rid)
            if not r:
                return {'ok': False, 'error': '房间不存在'}
            url, cfg = r.url, dict(self.cfg)
        ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg:
            return {'ok': False, 'error': '系统未找到 ffmpeg'}
        if url.startswith('demo://'):
            stream, anchor = 'demo://testsrc', r.display_name
        else:
            try:
                info = resolve(url, cfg)
            except ResolveError as e:
                return {'ok': False, 'error': f'{e.kind} · {e.detail}'}
            if not info.stream_url:
                return {'ok': False, 'error': '当前未开播，没有可预览的画面'}
            stream, anchor = info.stream_url, (r.alias or info.anchor)
        out_dir = self.base / 'preview'
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f'{rid}.mp4'
        try:
            out.unlink(missing_ok=True)
        except Exception:
            pass
        if stream.startswith('demo://'):
            cmd = [ffmpeg, '-y', '-loglevel', 'error', '-f', 'lavfi',
                   '-i', 'testsrc=size=640x360:rate=20', '-t', str(seconds),
                   '-c:v', 'libx264', '-preset', 'veryfast', '-pix_fmt', 'yuv420p',
                   '-movflags', '+faststart', str(out)]
        else:
            headers = f'User-Agent: {UA}\r\n'
            ref = _referer_for(url)
            if ref:
                headers = f'Referer: {ref}\r\n' + headers
            cmd = [ffmpeg, '-y', '-loglevel', 'error', '-rw_timeout', '12000000',
                   '-headers', headers, '-i', stream,
                   '-t', str(seconds), '-c:v', 'libx264', '-preset', 'veryfast',
                   '-vf', 'scale=-2:480', '-c:a', 'aac', '-b:a', '64k',
                   '-movflags', '+faststart', str(out)]
        creation = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=60,
                                  creationflags=creation)
        except subprocess.TimeoutExpired:
            return {'ok': False, 'error': '预览超时（流无响应）'}
        except Exception as e:
            return {'ok': False, 'error': f'{type(e).__name__}: {e}'}
        if not out.is_file() or out.stat().st_size < 1024:
            detail = (proc.stderr or b'').decode('utf-8', 'replace').strip().splitlines()
            return {'ok': False, 'error': f'生成预览失败：{detail[-1][:150] if detail else "无输出"}'}
        return {'ok': True, 'url': f'/preview/{rid}.mp4', 'anchor': anchor,
                'size': out.stat().st_size, 'ts': time.time()}

    def stream_info(self, rid: str) -> dict:
        """给界面「预览播放器」用：解析出浏览器可直接播的流地址。

        浏览器播放优先要 HLS（M3U8）：抖音 CDN 允许跨域（ACAO:*），
        界面用 hls.js 走 MSE 播放，与抖音网页播放器同一套路；
        录制仍走 FLV，两者的取舍见 _extract_stream。
        """
        with self.lock:
            r = self.rooms.get(rid)
            if not r:
                return {'ok': False, 'error': '房间不存在'}
            url, cfg = r.url, dict(self.cfg)
        if url.startswith('demo://'):
            return {'ok': False, 'error': '演示房间请用「生成预览」查看画面'}
        try:
            info = resolve(url, cfg, prefer='hls')
        except ResolveError as e:
            return {'ok': False, 'error': f'{e.kind} · {e.detail}'}
        if not info.stream_url:
            return {'ok': False, 'error': '当前未开播，没有可播放的画面'}
        s = info.stream_url
        kind = 'hls' if '.m3u8' in s else ('flv' if '.flv' in s else 'other')
        return {'ok': True, 'kind': kind, 'url': s,
                'anchor': r.alias or info.anchor, 'title': info.title}

    def export_rooms(self, directory: str, filename: str) -> dict:
        """把房间列表导出成文本（格式与导入一致，可直接再导入）。

        由用户指定目录和文件名：浏览器直接下载会默默掉进「下载」文件夹，
        用户根本不知道去哪了，所以走后端写文件 + 界面自带的目录浏览器。
        """
        with self.lock:
            lines = []
            for r in self.rooms.values():
                name = (r.alias or r.anchor or '').strip()
                note = (r.note or '').strip()
                who = ('主播:' + name) if name else note
                parts = [r.url]
                if r.quality and r.quality != '原画':
                    parts.insert(0, r.quality)
                if who:
                    parts.append(who)
                lines.append(','.join(parts))
        if not lines:
            return {'ok': False, 'error': '还没有直播间可导出'}
        name = _safe_dir_name(filename or '').strip() or '直播间列表.txt'
        if not name.lower().endswith('.txt'):
            name += '.txt'
        try:
            d = _norm_dir(directory)
            if not d.is_absolute():
                d = self.base / d
            d.mkdir(parents=True, exist_ok=True)
            p = d / name
            p.write_text('\n'.join(lines) + '\n', encoding='utf-8-sig')
        except Exception as e:
            return {'ok': False, 'error': f'写入失败：{e}'}
        return {'ok': True, 'path': str(p), 'count': len(lines)}

    def _start_record(self, r: Room, stream_url: str):
        ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg:
            r.status = 'error'
            r.error = 'NO_FFMPEG · 系统未找到 ffmpeg'
            self.log('error', '未找到 ffmpeg，跳过录制')
            return
        path = self._record_path(r)
        path.parent.mkdir(parents=True, exist_ok=True)
        logdir = self.base / 'logs'
        logdir.mkdir(parents=True, exist_ok=True)
        self.err_paths[r.id] = logdir / f'ffmpeg_{r.id}.log'
        try:
            err_file = open(self.err_paths[r.id], 'ab')
        except Exception:
            err_file = subprocess.DEVNULL
        if stream_url.startswith('demo://'):
            # -re 按真实速度产出：否则 20 秒的测试流 1 秒就编完，看不出录制过程
            cmd = [ffmpeg, '-y', '-loglevel', 'error', '-re', '-f', 'lavfi',
                   '-i', 'testsrc=size=320x240:rate=15', '-t', '20',
                   '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
                   '-f', 'mpegts', str(path)]
        else:
            # 抖音 CDN 会校验 Referer，缺了会 403；快手不校验，但按平台带上更稳
            headers = f'User-Agent: {UA}\r\n'
            ref = _referer_for(r.url)
            if ref:
                headers = f'Referer: {ref}\r\n' + headers
            cmd = [ffmpeg, '-y', '-loglevel', 'error',
                   '-rw_timeout', '15000000',
                   '-reconnect', '1', '-reconnect_streamed', '1',
                   '-reconnect_at_eof', '1', '-reconnect_delay_max', '10',
                   '-headers', headers,
                   '-i', stream_url, '-c', 'copy', '-f', 'mpegts', str(path)]
        try:
            creation = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                   stdout=subprocess.DEVNULL, stderr=err_file,
                                   creationflags=creation)
        except Exception as e:
            r.status = 'error'
            r.error = f'FFMPEG_START · {type(e).__name__}: {e}'
            self.log('error', f'[{r.anchor}] 启动 ffmpeg 失败: {e}')
            if err_file not in (subprocess.DEVNULL, None):
                err_file.close()
            return
        finally:
            if err_file not in (subprocess.DEVNULL, None):
                try:
                    err_file.close()
                except Exception:
                    pass
        # 让进程持有独立句柄，避免与上面 finally 抢关闭
        self.processes[r.id] = proc
        r.status = 'recording'
        r.file = self._rel(path)
        r.started_at = time.time()
        r.rec_bytes = 0
        r.rec_seconds = 0
        self._seg_start[r.id] = time.time()
        self._last_stream[r.id] = stream_url
        self.log('ok', f'[{r.anchor}] 开始录制 → {r.file}')

    def _stop_record(self, rid: str):
        proc = self.processes.pop(rid, None)
        r = self.rooms.get(rid)
        if proc and proc.poll() is None:
            # 先试 q（有的 ffmpeg 读 stdin，能优雅收尾）；管道 stdin 多半不生效，
            # 所以只等 0.8 秒就转 terminate。等 3 秒会让分段各长 5~8 秒。
            try:
                if proc.stdin:
                    proc.stdin.write(b'q')
                    proc.stdin.close()
                proc.wait(timeout=0.8)
            except Exception:
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        if r and r.status == 'recording':
            r.status = 'live'
            if r.started_at:
                r.rec_seconds = max(r.rec_seconds, time.time() - r.started_at)
            self.log('info', f'[{r.anchor}] 已停止录制')
        self._schedule_remux(rid)

    # ---------- 转封装 ----------
    def _schedule_remux(self, rid: str):
        """录制结束后在后台把 ts 转成 mp4（不阻塞停止动作）。"""
        if not self.cfg.get('auto_mp4', True):
            return
        with self.lock:
            r = self.rooms.get(rid)
            if not r or not r.file:
                return
            rel = r.file
        threading.Thread(target=self._remux_later, args=(rid, rel), daemon=True,
                         name=f'remux-{rid}').start()

    def _remux_later(self, rid: str, rel: str):
        # 等 ffmpeg 彻底释放文件句柄再转，避免 Windows 上「文件被占用」
        time.sleep(1.5)
        self._remux(rid, rel)

    def _remux(self, rid: str, rel: str, force: bool = False) -> dict:
        """把已录制的 ts 无损转封装成 mp4。

        `-c copy` 不重新编码，几秒完成；失败（如编码不被 mp4 支持）就保留原件，
        保证「至少还有能播的 ts」，不会因为转码把录像弄丢。
        """
        src = self._abs(rel)
        if not src.is_file():
            return {'ok': False, 'error': '文件不存在'}
        if src.suffix.lower() == '.mp4':
            return {'ok': True, 'file': rel}
        ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg:
            return {'ok': False, 'error': '系统未找到 ffmpeg'}
        if not force and src.stat().st_size < 128 * 1024:
            return {'ok': False, 'error': '文件过小，跳过转码'}
        dst = src.with_suffix('.mp4')
        cmd = [ffmpeg, '-y', '-loglevel', 'error', '-i', str(src),
               '-c', 'copy', '-movflags', '+faststart', str(dst)]
        creation = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=600,
                                  creationflags=creation)
        except Exception as e:
            self.log('warn', f'转码异常: {type(e).__name__}: {e}')
            return {'ok': False, 'error': str(e)}
        if proc.returncode != 0 or not dst.is_file() or dst.stat().st_size < 1024:
            detail = (proc.stderr or b'').decode('utf-8', 'replace').strip().splitlines()
            self.log('warn', f'[{rid}] 转 mp4 失败，保留原文件：'
                             f'{detail[-1][:120] if detail else "无输出"}')
            dst.unlink(missing_ok=True)
            return {'ok': False, 'error': 'ffmpeg 转封装失败'}
        size = dst.stat().st_size / 1048576
        if not self.cfg.get('keep_ts', False):
            try:
                src.unlink()
            except Exception:
                pass
        with self.lock:
            r = self.rooms.get(rid)
            if r and r.file == rel:
                r.file = self._rel(dst)
        self.log('ok', f'已转 mp4：{dst.name}（{size:.1f} MB）')
        return {'ok': True, 'file': self._rel(dst), 'size': size}

    def remux(self, rid: str) -> dict:
        with self.lock:
            r = self.rooms.get(rid)
            if not r or not r.file:
                return {'ok': False, 'error': '没有可转换的录像'}
            rel = r.file
        return self._remux(rid, rel, force=True)

    def stop_record(self, rid: str) -> bool:
        with self.lock:
            if rid not in self.processes:
                return False
            self._stop_record(rid)
            return True

    def start_record(self, rid: str) -> dict:
        """手动开始录制：立刻解析一次流地址并拉起 ffmpeg，不等下一轮检测。

        停止录制后房间会停在「直播中」，调度线程要等一个间隔（默认 180s）
        才会重新开录；这个接口让用户点一下就能马上继续录。
        """
        with self.lock:
            r = self.rooms.get(rid)
            if not r:
                return {'ok': False, 'error': '房间不存在'}
            if rid in self.processes:
                return {'ok': False, 'error': '已在录制中'}
            url, cfg = r.url, dict(self.cfg)
        try:
            info = resolve(url, cfg)
        except ResolveError as e:
            return {'ok': False, 'error': f'{e.kind} · {e.detail}'}
        except Exception as e:
            return {'ok': False, 'error': f'{type(e).__name__}: {e}'}
        with self.lock:
            r = self.rooms.get(rid)
            if not r:
                return {'ok': False, 'error': '房间不存在'}
            if info.anchor:
                r.anchor = info.anchor
            r.title = info.title or r.title
            r.viewers = info.viewers
            r.last_check = time.time()
            if not info.is_live or not info.stream_url:
                r.offline_hits += 1
                r.status = 'offline'
                return {'ok': False, 'error': '主播当前未开播，无法开始录制'}
            r.offline_hits = 0
            r.last_live = time.time()
            r.error = ''
            r.status = 'live'
            self._start_record(r, info.stream_url)
            name = r.anchor or r.display_name
        self.log('info', f'[{name}] 手动开始录制')
        return {'ok': True}

    def reap(self):
        """进程回收统一交给调度线程的 _watch_processes。

        原实现会在每次 state() 请求时抢先 pop 进程并置为 live，
        导致调度线程拿不到退出码、也不知道 ffmpeg 已崩，两处逻辑互相打架。
        """
        return

    # ---------- 状态 ----------
    STATUS_ORDER = {'recording': 0, 'live': 1, 'checking': 2,
                    'error': 3, 'idle': 4, 'offline': 5}

    def _sort_rooms(self):
        """在播的排前面，一直不播的沉底。

        offline 内部再按「最近一次在播时间」倒序：刚播过的靠上，
        从来没播过的排最后，方便一眼看出哪些房间该清掉。
        """
        def key(r: Room):
            rank = self.STATUS_ORDER.get(r.status, 9)
            recent = -r.last_live if rank >= 4 else 0.0
            return (rank, recent, r.display_name.lower())
        return sorted(self.rooms.values(), key=key)

    def state(self) -> dict:
        self.reap()
        with self.lock:
            rooms = []
            for r in self._sort_rooms():
                d = asdict(r)
                d['display_name'] = r.display_name
                d['platform'] = platform_of(r.url)
                rooms.append(d)
            stats = {
                'total': len(rooms),
                'enabled': sum(1 for r in rooms if r['enabled']),
                'live': sum(1 for r in rooms if r['status'] in ('live', 'recording')),
                'recording': sum(1 for r in rooms if r['status'] == 'recording'),
                'error': sum(1 for r in rooms if r['status'] == 'error'),
                'offline': sum(1 for r in rooms if r['status'] == 'offline'),
            }
            return {'running': self.running, 'stats': stats, 'rooms': rooms,
                    'config': dict(self.cfg), 'logs': list(self.logs)[-120:],
                    'save_path': str(self.save_root()),
                    'ffmpeg': bool(shutil.which('ffmpeg')),
                    'pace': {k: pace_gap(k) for k in _PACE_BASE},
                    'blocked': {k: round(platform_blocked(k)) for k in _PACE_BASE}}
