# -*- coding: utf-8 -*-
"""离线端到端自检：不访问任何直播平台。

启动本地服务 → 导入 demo:// 房间 → 立即检测 → ffmpeg 生成 20 秒测试片 →
校验文件 → 收尾清理。用于验证软件本身可用，不改动用户房间列表。
运行：python selftest.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
PORT = 8791
URL = f'http://127.0.0.1:{PORT}'

# 数据目录整个指到临时目录：rooms.json / config.json / downloads 都不碰用户数据。
# 必须在 import core 之前设好，否则 Engine 会按程序目录读写。
DATA_HOME = Path(tempfile.mkdtemp(prefix='lrs_selftest_'))
os.environ['LRS_HOME'] = str(DATA_HOME)

sys.path.insert(0, str(BASE))


def unit_checks() -> None:
    """纯离线验证解析与调度逻辑，不访问任何网络。"""
    from core import (Engine, Room, _anchor_of, _extract_stream,
                      _parse_room_store, clean_anchor_name)

    # 1) 主播名清洗：emoji、装饰符、零宽字符都要去掉，正常名字不能改坏
    cases = {
        '懒懒ᰔᩚএ᭄': '懒懒',
        '小明🔥🔥': '小明',
        'ʚ薇薇ɞ': '薇薇',
        '张三\u200b李四': '张三李四',
        '喆舞玥彤': '喆舞玥彤',
        'A B_C-1': 'A B_C-1',
    }
    for raw, want in cases.items():
        got = clean_anchor_name(raw, '兜底')
        assert got == want, f'主播名清洗错误: {raw!r} -> {got!r}，期望 {want!r}'
    assert clean_anchor_name('🌸🌸', '兜底') == '兜底', '纯 emoji 应回退到兜底名'

    # 2) 解析：主播名在 roomInfo.anchor、room.owner 两处都要能取到
    html = ('{"roomStore":{"roomInfo":{"room":{"status":2,"title":"测试标题",'
            '"stream_url":{"flv_pull_url":{"FULL_HD1":"http://cdn/x.flv?a=1\\u0026b=2"}}},'
            '"anchor":{"nickname":"懒懒🔥"}}},"linkmicStore":{}}')
    info = _parse_room_store(html)
    assert _anchor_of(info, html, 'rid') == '懒懒🔥', 'roomInfo.anchor 未取到昵称'
    assert clean_anchor_name(_anchor_of(info, html, 'rid')) == '懒懒'
    stream = _extract_stream(info['room'])
    assert stream.endswith('a=1&b=2'), f'流地址转义未还原: {stream}'

    html_owner = html.replace('"anchor":', '"user":').replace('"nickname"', '"nickname"')
    info2 = _parse_room_store(html_owner)
    assert _anchor_of(info2, html_owner, 'rid') == '懒懒🔥', 'roomInfo.user 未取到昵称'

    # 3) 调度：新加房间必须立刻可检查，不能排到 interval 之后
    eng = Engine()
    eng._save_rooms = lambda: None      # 自检不写用户房间文件
    saved_rooms = {k: v for k, v in eng.rooms.items()}
    saved_next = dict(eng.next_check)
    try:
        eng.rooms.clear()
        eng.next_check.clear()
        eng.add_rooms(['demo://自检A', 'demo://自检B'])
        assert len(eng.rooms) == 2
        for rid in eng.rooms:
            assert eng.next_check.get(rid, 1) == 0, '新房间应标记为立即检查'
        # 启动后若排期超过首轮窗口，应被重排到 12 秒内
        eng.next_check = {rid: time.time() + 500 for rid in eng.rooms}
        eng.cfg['interval'] = 180
        eng.start()
        now = time.time()
        for rid in eng.rooms:
            assert eng.next_check[rid] - now <= 13, '启动后首轮排期未收敛'
        eng.stop()
    finally:
        eng.rooms, eng.next_check = saved_rooms, saved_next
        eng.pool.shutdown(wait=False)

    print('      OK → 主播名清洗 7 例、解析双路径、首轮调度')


def call(path, payload=None, method=None):
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    req = urllib.request.Request(URL + path, data=data, method=method or ('POST' if data else 'GET'),
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode('utf-8'))


def main():
    data_home = DATA_HOME          # 与 unit_checks 共用同一个临时数据目录
    created = []
    proc = subprocess.Popen([sys.executable, str(BASE / 'app.py'), '--no-browser', '--port', str(PORT)],
                            cwd=str(BASE), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding='utf-8', errors='replace')
    ok = False
    try:
        print('[1/8] 核心逻辑（主播名清洗 / 解析双路径 / 首轮调度）')
        unit_checks()

        for _ in range(40):
            try:
                call('/api/state')
                break
            except Exception:
                time.sleep(0.25)
        else:
            raise RuntimeError('服务未启动')

        print('[2/8] 服务与静态页面')
        for path in ('/', '/style.css', '/app.js'):
            with urllib.request.urlopen(URL + path, timeout=5) as r:
                assert r.status == 200 and len(r.read()) > 200, path
        print('      OK')

        print('[3/8] 导入演示房间')
        r = call('/api/rooms', {'text': 'demo://自检演示'})
        assert r['added'] == 1, r
        state = call('/api/state')
        rid = next(x['id'] for x in state['rooms'] if x['url'] == 'demo://自检演示')
        assert state['ffmpeg'], '系统未找到 ffmpeg'
        print('      OK')

        print('[4/8] 启动引擎并立即检测')
        call('/api/engine', {'action': 'start'})
        call(f'/api/rooms/{rid}/check', {})
        time.sleep(4)
        room = next(x for x in call('/api/state')['rooms'] if x['id'] == rid)
        assert room['status'] in ('recording', 'live'), room
        assert room['anchor'] == '自检演示', f"主播名未解析: {room['anchor']!r}"
        assert room['file'], '未生成录制路径'
        created.append(data_home / room['file'])
        print('      OK →', room['status'], room['file'])

        print('[5/8] 等待 ffmpeg 产出文件')
        target = created[0]
        for _ in range(60):
            if target.is_file() and target.stat().st_size > 1024:
                break
            time.sleep(0.6)
        assert target.is_file() and target.stat().st_size > 1024, f'未产出文件: {target}'
        print(f'      OK → {target.stat().st_size / 1024:.0f} KB')

        print('[6/8] 状态与日志无异常')
        state = call('/api/state')
        assert state['stats']['error'] == 0, state['stats']
        assert state['running'] is True
        assert state['logs'], '日志为空'
        print(f"      OK → 日志 {len(state['logs'])} 条，异常 0")

        print('[7/8] 停止引擎')
        call('/api/engine', {'action': 'stop'})
        assert call('/api/state')['running'] is False
        print('      OK')

        print('[8/8] 停止后自动转 MP4')
        folder = target.parent
        for _ in range(40):
            if list(folder.glob('*.mp4')):
                break
            time.sleep(0.5)
        mp4s = list(folder.glob('*.mp4'))
        assert mp4s, f'未生成 mp4（目录: {folder}）'
        assert not list(folder.glob('*.ts')), '默认应删除原始 ts（keep_ts=False）'
        created.append(mp4s[0])
        print(f'      OK → {mp4s[0].name} {mp4s[0].stat().st_size / 1024:.0f} KB（原 ts 已清理）')
        ok = True
    finally:
        try:
            call('/api/engine', {'action': 'stop'})
        except Exception:
            pass
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=5)
            if 'Traceback' in out:
                print('[警告] 服务端出现 Traceback:\n', out[-800:])
        except subprocess.TimeoutExpired:
            proc.kill()
        for f in created:
            f.unlink(missing_ok=True)
        # 自检产物可能因转码改名，按目录兜底清理
        demo_dir = data_home / 'downloads' / '演示' / '自检演示'
        if not demo_dir.is_dir():
            demo_dir = data_home / 'downloads' / '自检演示'
        if demo_dir.is_dir():
            for f in demo_dir.rglob('*'):
                if f.is_file():
                    f.unlink(missing_ok=True)
        for f in (data_home / 'preview').glob('*.mp4'):
            f.unlink(missing_ok=True)
        shutil.rmtree(data_home, ignore_errors=True)
    print('\n结果:', '全部通过' if ok else '失败')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
