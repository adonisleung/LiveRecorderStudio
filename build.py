# -*- coding: utf-8 -*-
"""打包 LiveRecorder Studio 为 Windows onedir exe（纯标准库，体积小）。

运行：python build.py
产物：dist/LiveRecorderStudio/LiveRecorderStudio.exe
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# LRS_DISTPATH 可改输出根目录：旧 dist 被资源管理器/进程占用时换目录构建
DIST = Path(os.environ.get('LRS_DISTPATH', str(ROOT / 'dist'))) / 'LiveRecorderStudio'


# 这些是用户数据，重建时必须原样带过来（否则升级一次丢一次房间列表）
CARRY = ('rooms.json', 'config.json')


def _read_carry() -> dict:
    data = {}
    for name in CARRY:
        f = DIST / name
        if f.is_file():
            try:
                data[name] = f.read_bytes()
            except Exception as e:
                print(f'读取 {name} 失败：{e}')
    return data


def _park_old_dist():
    """把上一次的产物挪到临时目录，而不是删掉。

    PyInstaller 重建前会 rmtree 目标目录，几十个文件会撞上删除保护；
    用「移走」代替「删除」，构建不会被打断，旧文件交给系统清理临时目录。
    """
    if not DIST.exists():
        return
    target = Path(tempfile.gettempdir()) / f'LRS_old_{int(time.time())}'
    try:
        os.replace(str(DIST), str(target))
        print(f'旧产物已移到 {target}')
    except Exception as e:
        print(f'旧产物移动失败（继续尝试构建）：{e}')


def main():
    carry = _read_carry()
    _park_old_dist()
    cmd = [
        sys.executable, '-m', 'PyInstaller',
        '--noconfirm', '--clean', '--onedir',
        # 不弹控制台：桌面窗口内嵌 WebView2，所有状态都在界面里
        '--windowed',
        '--name', 'LiveRecorderStudio',
        '--distpath', str(DIST.parent),
        '--workpath', str(ROOT / '.build'),
        '--specpath', str(ROOT / '.build'),
        '--add-data', f'{ROOT}/static;static',
        '--add-data', f'{ROOT}/icon.ico;.',
        '--icon', str(ROOT / 'icon.ico'),
        # 内嵌窗口 + 托盘/热键
        '--hidden-import', 'tray',
        '--hidden-import', 'webview',
        '--hidden-import', 'webview.platforms.edgechromium',
        '--collect-all', 'webview',
        '--hidden-import', 'pythonnet',
        '--hidden-import', 'clr_loader',
        str(ROOT / 'app.py'),
    ]
    print('=== 打包中 ===')
    r = subprocess.run(cmd, cwd=str(ROOT), text=True, encoding='utf-8',
                       errors='replace', stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    print('\n'.join(r.stdout.splitlines()[-8:]))
    if r.returncode != 0:
        raise SystemExit(f'打包失败: {r.returncode}')

    (DIST / 'downloads').mkdir(parents=True, exist_ok=True)
    for name in ('rooms.json', 'config.json'):
        src = ROOT / name
        if src.is_file():
            shutil.copy2(src, DIST / name)
    # 把用户数据写回新产物
    for name, blob in carry.items():
        try:
            (DIST / name).write_bytes(blob)
            print(f'已保留 {name}（{len(blob)} 字节）')
        except Exception as e:
            print(f'写回 {name} 失败：{e}')

    exe = DIST / 'LiveRecorderStudio.exe'
    static = DIST / '_internal' / 'static' / 'index.html'
    size = sum(f.stat().st_size for f in DIST.rglob('*') if f.is_file()) / 1024 / 1024
    print(f'=== 校验 ===\nexe: {exe.is_file()} | 静态资源: {static.is_file()} | 体积: {size:.1f} MB')
    if not (exe.is_file() and static.is_file()):
        raise SystemExit('产物不完整')
    print('完成：双击 dist/LiveRecorderStudio/LiveRecorderStudio.exe 即可，'
          '启动后是独立桌面窗口，Alt+F1 隐藏 / 还原。')


if __name__ == '__main__':
    main()
