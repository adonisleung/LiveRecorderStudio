# -*- coding: utf-8 -*-
"""生成软件图标：复刻界面左上角的品牌标记（深色圆角方块 + 青色圆点 + 柔光外圈）。

纯标准库实现：自己光栅化 + 自写 PNG/ICO 编码，不依赖 Pillow。
输出：icon.ico（多尺寸）、static/icon.ico（页面 favicon）、icon_preview.png（预览）

运行：python make_icon.py
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SIZES = (16, 24, 32, 48, 64, 128, 256)
SS = 4                      # 超采样倍数，用于抗锯齿

BG = (18, 21, 26)           # #12151A 面板
BORDER = (45, 51, 61)       # #2D333D 描边
ACCENT = (94, 234, 212)     # #5EEAD4 主色


def _blend(dst, src, a):
    """把 src 以 alpha a 叠加到 dst（都是 0-255 三元组），返回新颜色。"""
    return tuple(int(round(dst[i] * (1 - a) + src[i] * a)) for i in range(3))


def _in_round_rect(x, y, w, h, r):
    """点是否落在圆角矩形内（坐标已归一化到 0..1）。"""
    cx = min(max(x, r), w - r)
    cy = min(max(y, r), h - r)
    dx, dy = x - cx, y - cy
    return dx * dx + dy * dy <= r * r


def render(size: int) -> bytes:
    """渲染一张 size×size 的 RGBA 图，返回原始字节（逐行）。"""
    big = size * SS
    # 先在超采样网格上算 alpha 覆盖率
    px = bytearray(big * big * 4)

    inset = big * 0.035
    radius = big * 0.23
    stroke = max(1.0, big * 0.012)
    dot_r = big * 0.125
    ring_r = big * 0.215        # 对应界面里的 box-shadow 外圈
    glow_r = big * 0.30         # 再往外一层极淡的柔光
    c = big / 2.0 - 0.5

    for y in range(big):
        for x in range(big):
            fx, fy = x + 0.5, y + 0.5
            # 圆角方块外轮廓（含描边）与内部填充
            outer = _in_round_rect(fx - inset, fy - inset, big - 2 * inset,
                                   big - 2 * inset, radius)
            inner = _in_round_rect(fx - inset - stroke, fy - inset - stroke,
                                   big - 2 * inset - 2 * stroke,
                                   big - 2 * inset - 2 * stroke, max(1.0, radius - stroke))
            dist = ((fx - c) ** 2 + (fy - c) ** 2) ** 0.5

            r = g = b = a = 0.0
            if outer:
                r, g, b = BORDER
                a = 1.0
                if inner:
                    r, g, b = BG
                    # 光晕：贴着圆点是一圈实心淡青（界面里的 box-shadow），
                    # 再往外极缓地淡出，避免出现「黑缝」
                    if dist > dot_r:
                        if dist <= ring_r:
                            r, g, b = _blend((int(r), int(g), int(b)), ACCENT, 0.15)
                        elif dist <= glow_r:
                            t = (glow_r - dist) / (glow_r - ring_r)
                            r, g, b = _blend((int(r), int(g), int(b)), ACCENT, 0.06 * t)
                    if dist <= dot_r:
                        r, g, b = ACCENT
                        a = 1.0
            o = (y * big + x) * 4
            px[o:o + 4] = bytes((int(r), int(g), int(b), int(a * 255)))

    # 盒式滤波下采样到目标尺寸
    out = bytearray(size * size * 4)
    n = SS * SS
    for y in range(size):
        for x in range(size):
            rr = gg = bb = aa = 0
            for sy in range(SS):
                for sx in range(SS):
                    o = ((y * SS + sy) * big + (x * SS + sx)) * 4
                    rr += px[o]; gg += px[o + 1]; bb += px[o + 2]; aa += px[o + 3]
            o = (y * size + x) * 4
            out[o:o + 4] = bytes((rr // n, gg // n, bb // n, aa // n))
    return bytes(out)


def to_png(size: int, rgba: bytes) -> bytes:
    """把 RGBA 原始数据编码成 PNG。"""
    raw = bytearray()
    stride = size * 4
    for y in range(size):
        raw.append(0)                       # 每行的 filter type = None
        raw += rgba[y * stride:(y + 1) * stride]

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack('>I', len(data)) + tag + data
                + struct.pack('>I', zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack('>IIBBBBB', size, size, 8, 6, 0, 0, 0)   # 8bit RGBA
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr)
            + chunk(b'IDAT', zlib.compress(bytes(raw), 9)) + chunk(b'IEND', b''))


def build_ico(entries: list[tuple[int, bytes]]) -> bytes:
    """entries: [(size, png_bytes), ...] → ICO 文件字节。"""
    head = struct.pack('<HHH', 0, 1, len(entries))
    offset = 6 + 16 * len(entries)
    dirs, blobs = b'', b''
    for size, data in entries:
        dirs += struct.pack('<BBBBHHII', size % 256, size % 256, 0, 0, 1, 32,
                            len(data), offset + len(blobs))
        blobs += data
    return head + dirs + blobs


def main():
    entries = []
    for s in SIZES:
        entries.append((s, to_png(s, render(s))))
    ico = build_ico(entries)
    (ROOT / 'icon.ico').write_bytes(ico)
    (ROOT / 'static' / 'icon.ico').write_bytes(ico)
    # 页面左上角 logo 与 exe/托盘同一张图，避免界面画一个、图标是另一个
    (ROOT / 'static' / 'icon.png').write_bytes(dict(entries)[256])
    # 顺带落一张 256 预览，方便肉眼确认
    (ROOT / 'icon_preview.png').write_bytes(dict(entries)[256])
    print(f'icon.ico 已生成：{len(ico) / 1024:.1f} KB，尺寸 {SIZES}')


if __name__ == '__main__':
    main()
