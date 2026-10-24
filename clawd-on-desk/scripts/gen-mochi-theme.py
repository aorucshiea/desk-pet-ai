#!/usr/bin/env python
"""gen-mochi-theme.py — 生成默认主题 mochi（麻薯猫）的全部像素画 GIF。

原创美术：代码程序化生成的像素猫，不包含任何官方主题资产。
可重复运行：python gen-mochi-theme.py  → 重建 themes/mochi/assets/。

画布约定与 cybercat 相同（45×45 viewBox、objectScale 布局），几何代码
无需改动。每帧在 45×45 的网格上用 1 格=1 像素块绘制，导出时 ×4
（180×180），GIF 透明背景、循环播放。
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image

OUT = Path(__file__).resolve().parent.parent / "themes" / "mochi" / "assets"
SCALE = 4
GRID = 45

# 调色板（奶油橘猫）
CREAM = (255, 236, 200)       # 主体
CREAM_D = (240, 213, 166)     # 主体阴影/描边内
ORANGE = (244, 156, 70)       # 耳朵/条纹
ORANGE_D = (214, 122, 46)     # 深条纹
OUTLINE = (92, 62, 46)        # 描边
EYE = (56, 42, 38)            # 眼睛
BLUSH = (250, 178, 150)       # 腮红
WHITE = (255, 254, 250)       # 高光/白
RED = (232, 90, 84)           # 错误
BLUE = (110, 168, 240)        # Zzz/水滴
YELLOW = (250, 208, 90)       # 星/问号
GREEN = (128, 196, 120)       # 对钩
PINK = (246, 158, 200)        # 爱心


class Frame:
    """一帧 45×45 像素画。"""

    def __init__(self) -> None:
        self.px: dict[tuple[int, int], tuple[int, int, int]] = {}

    def put(self, x: int, y: int, c: tuple[int, int, int]) -> None:
        x, y = round(x), round(y)
        if 0 <= x < GRID and 0 <= y < GRID:
            self.px[(x, y)] = c

    def rect(self, x: int, y: int, w: int, h: int, c: tuple[int, int, int]) -> None:
        for i in range(round(w)):
            for j in range(round(h)):
                self.put(x + i, y + j, c)

    def hline(self, x: int, y: int, w: int, c: tuple[int, int, int]) -> None:
        self.rect(x, y, w, 1, c)

    def vline(self, x: int, y: int, h: int, c: tuple[int, int, int]) -> None:
        self.rect(x, y, 1, h, c)

    # ── 身体部件（麻薯形圆猫）──────────────────────────────────────
    def body(self, cx: float, base_y: int, squash: float = 1.0, ear_l: str = "up",
             ear_r: str = "up", tail: int = 0) -> None:
        """椭圆麻薯身体：cx 中心，base_y 底部，squash<1 压扁。"""
        rx, ry = 13 * squash, 11 / squash if squash < 1 else 11 * (2 - squash) * 0.5 + 5.5
        ry = 11 if squash == 1.0 else ry
        # 简化：squash 只做 ±1 像素的呼吸
        dy = 0 if squash >= 1.0 else 1
        rx = 13 if squash >= 1.0 else 14
        ry = 11 if squash >= 1.0 else 10
        for yy in range(-int(ry), int(ry) + 1):
            span = rx * math.sqrt(max(0.0, 1 - (yy * yy) / (ry * ry)))
            for xx in range(-int(span), int(span) + 1):
                edge = abs(xx) >= int(span) - 0.4 or abs(yy) >= int(ry) - 0.6
                self.put(cx + xx, base_y - int(ry) + yy + dy, OUTLINE if edge else CREAM)
        # 描边修正：顶部一行用主体色（描边太重会显脏）
        # 耳朵：三角
        for (ex, kind) in ((cx - 7, ear_l), (cx + 7, ear_r)):
            if kind == "flat":
                self.rect(ex - 2, base_y - int(ry) - dy - 2, 5, 3, CREAM)
                self.hline(ex - 2, base_y - int(ry) - dy - 3, 5, OUTLINE)
            else:
                for i in range(4):
                    self.put(ex - 1 - i // 2, base_y - int(ry) - dy - 1 - i, ORANGE if i >= 2 else CREAM)
                    self.put(ex + i // 2, base_y - int(ry) - dy - 1 - i, ORANGE if i >= 2 else CREAM)
                    if i >= 2:
                        self.put(ex - 2 - i // 2, base_y - int(ry) - dy - 1 - i, OUTLINE)
                        self.put(ex + 1 + i // 2, base_y - int(ry) - dy - 1 - i, OUTLINE)
        # 条纹（头顶三道）
        for sx in (-5, 0, 5):
            self.rect(cx + sx - 1, base_y - int(ry) - dy + 1, 2, 3, ORANGE_D if sx == 0 else ORANGE)
        # 尾巴
        tx = cx + 13
        for i, (ox, oy) in enumerate(((0, 0), (1, -1), (2, -2), (3, -2), (4, -3))):
            lift = tail  # -1..1 摆动
            self.rect(tx + ox, base_y - 6 + oy + lift, 2, 2, ORANGE if i < 3 else ORANGE_D)

    def face(self, cx: float, base_y: int, eyes: str = "open", mouth: str = "cat",
             blush: bool = True, dy: int = 0) -> None:
        ey = base_y - 6 + dy
        lx, rx = cx - 5, cx + 4
        if eyes == "open":
            self.rect(lx, ey, 2, 3, EYE)
            self.rect(rx, ey, 2, 3, EYE)
            self.put(lx, ey - 1, WHITE)
            self.put(rx, ey - 1, WHITE)
        elif eyes == "happy":  # ^ ^
            self.put(lx - 1, ey, EYE); self.put(lx, ey - 1, EYE); self.put(lx + 1, ey, EYE)
            self.put(rx - 1, ey, EYE); self.put(rx, ey - 1, EYE); self.put(rx + 1, ey, EYE)
        elif eyes == "closed":  # - -
            self.hline(lx - 1, ey, 3, EYE)
            self.hline(rx - 1, ey, 3, EYE)
        elif eyes == "sad":
            self.rect(lx, ey, 2, 2, EYE)
            self.rect(rx, ey, 2, 2, EYE)
            self.put(lx - 1, ey - 1, EYE)
            self.put(rx + 2, ey - 1, EYE)
        elif eyes == "x":
            for exx in (lx, rx):
                self.put(exx - 1, ey - 1, EYE); self.put(exx + 1, ey - 1, EYE)
                self.put(exx, ey, EYE)
                self.put(exx - 1, ey + 1, EYE); self.put(exx + 1, ey + 1, EYE)
        elif eyes == "spiral":  # @ @(专注)
            self.hline(lx - 1, ey - 1, 3, EYE); self.hline(lx - 1, ey + 1, 3, EYE)
            self.put(lx, ey, EYE)
            self.hline(rx - 1, ey - 1, 3, EYE); self.hline(rx - 1, ey + 1, 3, EYE)
            self.put(rx, ey, EYE)
        if blush:
            self.rect(cx - 9, ey + 2, 2, 1, BLUSH)
            self.rect(cx + 8, ey + 2, 2, 1, BLUSH)
        my = ey + 4
        if mouth == "cat":
            self.put(cx - 1, my, EYE); self.put(cx, my - 0, EYE)
            self.put(cx + 1, my, EYE)
            self.put(cx - 0, my + 1, EYE)
        elif mouth == "open":
            self.rect(cx - 1, my, 3, 2, EYE)
        elif mouth == "wavy":
            self.put(cx - 2, my, EYE); self.put(cx - 1, my + 1, EYE)
            self.put(cx, my, EYE); self.put(cx + 1, my + 1, EYE); self.put(cx + 2, my, EYE)
        elif mouth == "none":
            pass

    def legs(self, cx: float, base_y: int, spread: int = 5) -> None:
        for lx in (cx - spread, cx + spread - 1):
            self.rect(lx, base_y, 3, 2, CREAM)
            self.hline(lx, base_y + 2, 3, OUTLINE)

    # ── 小道具 ─────────────────────────────────────────────────────
    def bubble_zzz(self, x: int, y: int, phase: int) -> None:
        size = 1 + phase % 3
        zx, zy = x, y - phase
        # 粗像素 Z
        for i in range(size + 1):
            self.put(zx + i, zy, BLUE)
            self.put(zx + i, zy + size, BLUE)
            self.put(zx + i, zy + size - i, BLUE)

    def qmark(self, x: int, y: int, c=YELLOW) -> None:
        pts = [(0,0),(1,0),(2,0),(2,1),(2,2),(1,2),(1,3),(1,4),(1,6)]
        for dx, dy in pts:
            self.put(x + dx, y + dy, c)

    def bang(self, x: int, y: int, c=RED) -> None:
        self.vline(x, y, 4, c)
        self.vline(x + 1, y + 1, 3, c)
        self.rect(x - 1, y + 5, 3, 1, c)

    def sparkle(self, x: int, y: int, c=YELLOW) -> None:
        self.put(x, y - 1, c); self.put(x - 1, y, c); self.put(x + 1, y, c); self.put(x, y + 1, c)
        self.put(x, y, WHITE)

    def heart(self, x: int, y: int) -> None:
        for dx, dy in ((-2,-1),(-1,-2),(0,-1),(1,-2),(2,-1),(-2,0),(2,0),(-1,1),(1,1),(0,2),(-1,2) ,(1,2)):
            self.put(x + dx, y + dy, PINK)

    def ball(self, x: int, y: int, c=BLUE) -> None:
        self.rect(x - 1, y - 1, 3, 3, c)
        self.put(x - 1, y - 1, WHITE)

    def drop(self, x: int, y: int) -> None:
        self.put(x, y - 1, BLUE); self.rect(x - 1, y, 3, 2, BLUE); self.put(x, y + 2, BLUE)

    def box(self, cx: float, y: int) -> None:
        w = 14
        self.rect(cx - w // 2, y, w, 8, (206, 154, 104))
        self.hline(cx - w // 2, y, w, OUTLINE)
        self.hline(cx - w // 2, y + 7, w, OUTLINE)
        self.vline(cx - w // 2, y, 8, OUTLINE); self.vline(cx + w // 2 - 1, y, 8, OUTLINE)
        self.vline(cx - 1, y + 1, 6, (170, 120, 76))

    def broom(self, x: int, y: int, tilt: int) -> None:
        self.vline(x, y - 8 + tilt, 8, (150, 108, 70))
        self.rect(x - 2, y + tilt, 5, 3, YELLOW)
        self.hline(x - 2, y + 3 + tilt, 5, ORANGE_D)

    def dust(self, x: int, y: int) -> None:
        self.put(x, y, CREAM_D); self.put(x + 1, y - 1, CREAM_D); self.put(x - 1, y + 1, WHITE)

    def envelope(self, x: int, y: int) -> None:
        self.rect(x, y, 9, 6, WHITE)
        for i in range(9):
            self.put(x + i, y, OUTLINE)
            self.put(x + i, y + 5, OUTLINE)
        self.vline(x, y, 6, OUTLINE); self.vline(x + 8, y, 6, OUTLINE)
        for i in range(4):
            self.put(x + 1 + i, y + 1 + i, CREAM_D); self.put(x + 7 - i, y + 1 + i, CREAM_D)

    def steam(self, x: int, y: int) -> None:
        self.put(x, y, WHITE); self.put(x + 1, y - 1, WHITE); self.put(x, y - 2, WHITE)

    def bounce_offset(self, phase: int, n: int, amp: int = 2) -> int:
        return -round(amp * math.sin(2 * math.pi * phase / n))

    def image(self) -> Image.Image:
        img = Image.new("RGBA", (GRID, GRID), (0, 0, 0, 0))
        for (x, y), c in self.px.items():
            img.putpixel((x, y), c + (255,))
        return img.resize((GRID * SCALE, GRID * SCALE), Image.NEAREST)


def save_gif(name: str, frames: list[Frame], ms: int = 140) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    imgs = [f.image() for f in frames]
    imgs[0].save(
        OUT / f"mochi-{name}.gif",
        save_all=True,
        append_images=imgs[1:],
        duration=ms,
        loop=0,
        transparency=0,
        disposal=2,
    )
    print(f"mochi-{name}.gif  {len(frames)}f")


CX, BY = 22, 38  # 身体中心/底部


def _base(phase: int, n: int, squash_cycle: bool = True, **kw) -> Frame:
    f = Frame()
    squash = 1.0
    if squash_cycle:
        squash = 1.0 if phase % 2 == 0 else 0.9
    f.body(CX, BY, squash=squash, **kw)
    return f


# ── 各状态 ────────────────────────────────────────────────────────
def st_idle() -> None:
    n = 8
    frames = []
    for p in range(n):
        f = _base(p, n)
        f.legs(CX, BY)
        blink = p in (5, 6)
        f.face(CX, BY, eyes="closed" if blink else "open")
        frames.append(f)
    save_gif("idle", frames, 160)


def st_thinking() -> None:
    n = 8
    frames = []
    for p in range(n):
        f = _base(p, n, ear_l="flat", tail=(p % 4 - 1))
        f.legs(CX, BY)
        f.face(CX, BY, eyes="spiral", mouth="none")
        bob = f.bounce_offset(p, n, 1)
        f.qmark(33, 8 + bob)
        frames.append(f)
    save_gif("thinking", frames, 150)


def st_working() -> None:
    n = 8
    frames = []
    for p in range(n):
        f = _base(p, n, tail=(1 if p < 4 else 0))
        f.legs(CX, BY)
        f.face(CX, BY, eyes="open", mouth="none")
        # 小锤子敲敲
        hy = 14 + (0 if p % 2 == 0 else 3)
        f.rect(30, hy, 1, 6, (150, 108, 70))
        f.rect(28, hy - 2, 5, 3, (180, 180, 188))
        if p >= 4:
            f.drop(36, 10)
        frames.append(f)
    save_gif("working", frames, 130)


def st_juggling() -> None:
    n = 8
    frames = []
    for p in range(n):
        f = _base(p, n)
        f.legs(CX, BY)
        f.face(CX, BY, eyes="happy", mouth="open")
        ang = 2 * math.pi * p / n
        bx, by = CX + 12 * math.cos(ang), 14 - 8 * math.sin(ang)
        f.ball(round(bx), round(by), BLUE if p % 2 == 0 else YELLOW)
        f.ball(round(CX - 12 * math.cos(ang)), 14 - round(4 * math.sin(ang)), PINK)
        frames.append(f)
    save_gif("juggling", frames, 110)


def st_error() -> None:
    n = 6
    frames = []
    for p in range(n):
        shake = 1 if p % 2 == 0 else -1
        f = Frame()
        f.body(CX + shake, BY, ear_l="flat", ear_r="flat", tail=0)
        f.legs(CX + shake, BY)
        f.face(CX + shake, BY, eyes="x", mouth="wavy", blush=False)
        f.bang(CX + shake, 6, RED)
        frames.append(f)
    save_gif("error", frames, 120)


def st_attention() -> None:
    n = 8
    frames = []
    for p in range(n):
        f = Frame()
        up = f.bounce_offset(p, n, 2)
        f.body(CX, BY + up, tail=(1 if p < n // 2 else 0))
        f.legs(CX, BY + up)
        f.face(CX, BY + up, eyes="happy", mouth="open")
        f.sparkle(6, 10 + up)
        f.sparkle(38, 14 + up)
        if p in (2, 5):
            f.heart(32, 6)
        frames.append(f)
    save_gif("attention", frames, 120)


def st_notification() -> None:
    n = 8
    frames = []
    for p in range(n):
        f = _base(p, n, tail=(p % 3 - 1))
        f.legs(CX, BY)
        f.face(CX, BY, eyes="open", mouth="none")
        bob = f.bounce_offset(p, n, 1)
        f.envelope(28, 6 + bob)
        if p % 4 < 2:
            f.sparkle(26, 4 + bob)
        frames.append(f)
    save_gif("notification", frames, 140)


def st_sleeping() -> None:
    n = 10
    frames = []
    for p in range(n):
        f = Frame()
        # 趴睡：压扁的身体
        f.body(CX, BY, squash=0.9, ear_l="flat", ear_r="flat", tail=(p % 4 // 2))
        f.face(CX, BY, eyes="closed", mouth="none", blush=True, dy=1)
        f.bubble_zzz(32, 10, p)
        if p >= 5:
            f.bubble_zzz(37, 6, p - 5)
        frames.append(f)
    save_gif("sleeping", frames, 220)


def st_carrying() -> None:
    n = 8
    frames = []
    for p in range(n):
        f = Frame()
        wob = 1 if p % 2 == 0 else -1
        f.body(CX, BY, tail=(p % 3 - 1))
        f.legs(CX, BY, spread=6)
        f.face(CX, BY, eyes="closed", mouth="cat")
        f.box(CX + wob, 8)
        frames.append(f)
    save_gif("carrying", frames, 150)


def st_sweeping() -> None:
    n = 8
    frames = []
    for p in range(n):
        f = _base(p, n)
        f.legs(CX, BY, spread=6)
        f.face(CX, BY, eyes="closed", mouth="cat")
        tilt = 2 if p % 2 == 0 else -2
        f.broom(34, 30, tilt)
        f.dust(34 + tilt, 34)
        frames.append(f)
    save_gif("sweeping", frames, 130)


def st_dragging() -> None:
    n = 8
    frames = []
    for p in range(n):
        f = Frame()
        sway = 1 if p % 2 == 0 else -1
        f.body(CX, BY - 6, tail=(p % 3 - 1))
        f.face(CX, BY - 6, eyes="sad", mouth="wavy", blush=False)
        # 悬空的小腿
        for lx in (CX - 5, CX + 4):
            f.rect(lx + sway, BY + 5, 3, 3, CREAM)
            self_h = f
            self_h.hline(lx + sway, BY + 8, 3, OUTLINE)
        frames.append(f)
    save_gif("dragging", frames, 150)


def st_annoyed() -> None:
    n = 6
    frames = []
    for p in range(n):
        f = Frame()
        f.body(CX, BY, ear_l="flat", ear_r="flat", tail=1)
        f.legs(CX, BY)
        f.face(CX, BY, eyes="sad", mouth="wavy")
        # 鼓腮 + 生气符号
        f.rect(CX - 11, BY - 4, 3, 3, BLUSH)
        f.rect(CX + 9, BY - 4, 3, 3, BLUSH)
        if p % 3 < 2:
            # 井字怒符
            f.hline(33, 6, 5, RED); f.hline(33, 8, 5, RED); f.vline(34, 5, 5, RED); f.vline(36, 5, 5, RED)
        f.steam(8, 8); f.steam(12, 6)
        frames.append(f)
    save_gif("annoyed", frames, 130)


# ── 迷你模式（极简模式）：Q 版小麻薯 ──────────────────────────────
# 小身体（rx≈8），大头大眼，画在 45 网格中央偏下，内容占画布 ~60%。
def _mini_body(f: Frame, cx: float, base_y: int, squash: float = 1.0, ear_l: str = "up",
               ear_r: str = "up", tail: int = 0) -> None:
    rx = 8 if squash >= 1.0 else 9
    ry = 7 if squash >= 1.0 else 6
    dy = 0 if squash >= 1.0 else 1
    for yy in range(-ry, ry + 1):
        span = rx * math.sqrt(max(0.0, 1 - (yy * yy) / (ry * ry)))
        for xx in range(-int(span), int(span) + 1):
            edge = abs(xx) >= int(span) - 0.4 or abs(yy) >= ry - 0.6
            f.put(cx + xx, base_y - ry + yy + dy, OUTLINE if edge else CREAM)
    for (ex, kind) in ((cx - 5, ear_l), (cx + 5, ear_r)):
        if kind == "flat":
            f.rect(ex - 1, base_y - ry - dy - 1, 4, 2, CREAM)
            f.hline(ex - 1, base_y - ry - dy - 2, 4, OUTLINE)
        else:
            for i in range(3):
                f.put(ex - i // 2, base_y - ry - dy - i, ORANGE if i >= 1 else CREAM)
                f.put(ex + i // 2, base_y - ry - dy - i, ORANGE if i >= 1 else CREAM)
    for sx in (-3, 0, 3):
        f.rect(cx + sx - 1, base_y - ry - dy + 1, 2, 2, ORANGE)
    tx = cx + 8
    for i, (ox, oy) in enumerate(((0, 0), (1, -1), (2, -2))):
        f.rect(tx + ox, base_y - 4 + oy + tail, 2, 2, ORANGE if i < 2 else ORANGE_D)


def _mini_face(f: Frame, cx: float, base_y: int, eyes: str = "open", mouth: str = "cat",
               dy: int = 0) -> None:
    ey = base_y - 3 + dy
    lx, rx = cx - 4, cx + 3
    # Q 版大眼
    if eyes == "open":
        f.rect(lx, ey, 3, 4, EYE)
        f.rect(rx, ey, 3, 4, EYE)
        f.put(lx, ey - 1, WHITE); f.put(rx, ey - 1, WHITE)
    elif eyes == "happy":
        f.put(lx - 1, ey, EYE); f.put(lx, ey - 1, EYE); f.put(lx + 1, ey, EYE)
        f.put(rx - 1, ey, EYE); f.put(rx, ey - 1, EYE); f.put(rx + 1, ey, EYE)
    elif eyes == "closed":
        f.hline(lx - 1, ey, 3, EYE); f.hline(rx - 1, ey, 3, EYE)
    elif eyes == "alert":
        f.rect(lx, ey - 1, 3, 5, EYE); f.rect(rx, ey - 1, 3, 5, EYE)
        f.put(lx, ey - 2, WHITE); f.put(rx, ey - 2, WHITE)
    f.rect(cx - 6, ey + 3, 2, 1, BLUSH); f.rect(cx + 5, ey + 3, 2, 1, BLUSH)
    my = ey + 5
    if mouth == "cat":
        f.put(cx - 1, my, EYE); f.put(cx, my, EYE); f.put(cx + 1, my, EYE); f.put(cx, my + 1, EYE)
    elif mouth == "open":
        f.rect(cx - 1, my, 3, 2, EYE)


def st_mini_idle() -> None:
    n = 8
    frames = []
    for p in range(n):
        f = Frame()
        sq = 1.0 if p % 2 == 0 else 0.9
        _mini_body(f, CX, 40, squash=sq, tail=(p // 4))
        _mini_face(f, CX, 40, eyes="closed" if p in (5, 6) else "open")
        frames.append(f)
    save_gif("mini-idle", frames, 170)


def st_mini_working() -> None:
    n = 6
    frames = []
    for p in range(n):
        f = Frame()
        _mini_body(f, CX, 40, tail=(p % 2))
        _mini_face(f, CX, 40, eyes="closed", mouth="cat")
        # 小爪子飞快敲
        lx = CX - 5 if p % 2 == 0 else CX - 4
        f.rect(lx, 40, 3, 2, CREAM); f.hline(lx, 42, 3, OUTLINE)
        f.rect(CX + 3, 40, 3, 2, CREAM); f.hline(CX + 3, 42, 3, OUTLINE)
        if p % 3 == 2:
            f.sparkle(34, 24)
        frames.append(f)
    save_gif("mini-working", frames, 110)


def st_mini_enter() -> None:
    n = 10
    frames = []
    for p in range(n):
        f = Frame()
        # 从左侧弹跳滚入：位移 + 挤压回弹
        t01 = p / (n - 1)
        x = CX - 14 + 28 * (t01 ** 0.6)
        bounce = -round(4 * math.sin(math.pi * min(1.0, t01 * 1.6)))
        squash = 0.85 if p in (0, n - 2) else 1.0
        _mini_body(f, x, 40 + bounce, squash=squash, tail=(1 if t01 < 0.5 else 0))
        _mini_face(f, x, 40 + bounce, eyes="happy" if t01 > 0.5 else "open", mouth="open" if t01 > 0.5 else "cat")
        if p >= n - 3:
            f.sparkle(x + 9, 22 + bounce)
        frames.append(f)
    save_gif("mini-enter", frames, 90)


def st_mini_sleeping() -> None:
    n = 8
    frames = []
    for p in range(n):
        f = Frame()
        _mini_body(f, CX, 41, squash=0.9, ear_l="flat", ear_r="flat", tail=(p // 4))
        _mini_face(f, CX, 41, eyes="closed", dy=1)
        f.bubble_zzz(33, 18, p)
        frames.append(f)
    save_gif("mini-sleeping", frames, 240)


def st_mini_alert() -> None:
    n = 6
    frames = []
    for p in range(n):
        f = Frame()
        up = -1 if p % 2 == 0 else 0
        _mini_body(f, CX, 40 + up, tail=1 if p < 3 else 0)
        _mini_face(f, CX, 40 + up, eyes="alert", mouth="open")
        f.bang(CX, 22, RED)
        frames.append(f)
    save_gif("mini-alert", frames, 120)


def st_mini_happy() -> None:
    n = 8
    frames = []
    for p in range(n):
        f = Frame()
        up = -2 if p % 4 in (1, 2) else 0
        _mini_body(f, CX, 40 + up, tail=(1 if p < 4 else 0))
        _mini_face(f, CX, 40 + up, eyes="happy", mouth="open")
        if p % 4 < 2:
            f.heart(CX + 10, 24 + up)
        f.sparkle(CX - 11, 22 + up)
        frames.append(f)
    save_gif("mini-happy", frames, 130)


# ── 睡眠过渡（sleepSequence: full）───────────────────────────────
def st_yawning() -> None:
    n = 8
    frames = []
    for p in range(n):
        f = Frame()
        f.body(CX, BY, tail=0)
        f.legs(CX, BY)
        # 哈欠：眼睛眯起，嘴巴张大成 O，前半程张大后半程合上
        open_amt = min(p, n - 1 - p)  # 0..3..0
        f.face(CX, BY, eyes="closed", mouth="none")
        my = BY - 2
        w = 2 + open_amt
        h = 1 + open_amt
        f.rect(CX - w // 2, my, w, h, EYE)
        # 一只爪子捂嘴
        if open_amt >= 2:
            f.rect(CX + 2, my + 1, 3, 3, CREAM)
            f.hline(CX + 2, my + 4, 3, OUTLINE)
        # 小泪花
        if open_amt >= 3:
            f.drop(CX + 8, my - 3)
        frames.append(f)
    save_gif("yawning", frames, 160)


def st_dozing() -> None:
    n = 10
    frames = []
    for p in range(n):
        f = Frame()
        nod = 1 if p % 4 in (2, 3) else 0  # 点头
        f.body(CX, BY - nod * 0, tail=0)
        f.legs(CX, BY)
        f.face(CX, BY, eyes="closed", mouth="none", dy=nod)
        # 头顶困困气泡
        f.put(CX + 11, BY - 16, CREAM_D); f.put(CX + 12, BY - 17, CREAM_D)
        if p >= 5:
            f.bubble_zzz(CX + 10, BY - 20, p - 5)
        frames.append(f)
    save_gif("dozing", frames, 200)


def st_collapsing() -> None:
    n = 8
    frames = []
    for p in range(n):
        f = Frame()
        # 渐渐趴下：身体压低
        lower = min(p, 4)
        f.body(CX, BY, squash=0.9, ear_l="flat", ear_r="flat", tail=0)
        f.face(CX, BY, eyes="closed" if p >= 3 else "sad", mouth="none", dy=1)
        # 下沉的小星（晕）
        if p >= 2:
            f.sparkle(CX - 9, BY - 18 + lower, YELLOW)
            f.sparkle(CX + 9, BY - 16 + lower, YELLOW)
        frames.append(f)
    save_gif("collapsing", frames, 150)


def st_waking() -> None:
    n = 8
    frames = []
    for p in range(n):
        f = Frame()
        # 伸懒腰：前低后高再回正，眼睛慢慢睁开
        stretch = -1 if 1 <= p <= 3 else 0
        f.body(CX, BY + stretch, squash=0.95 if stretch else 1.0, tail=(1 if p > 4 else 0))
        f.legs(CX, BY)
        eyes = "closed" if p < 2 else ("sad" if p < 4 else "open")
        f.face(CX, BY + stretch, eyes=eyes, mouth="none")
        if 2 <= p <= 4:
            # 伸出的前爪
            f.rect(CX - 8, BY - 6 + stretch, 4, 2, CREAM)
            f.rect(CX + 4, BY - 6 + stretch, 4, 2, CREAM)
        if p >= 5:
            f.sparkle(CX - 10, BY - 16)
        frames.append(f)
    save_gif("waking", frames, 150)


if __name__ == "__main__":
    st_idle()
    st_thinking()
    st_working()
    st_juggling()
    st_error()
    st_attention()
    st_notification()
    st_sleeping()
    st_carrying()
    st_sweeping()
    st_dragging()
    st_annoyed()
    st_mini_idle()
    st_mini_working()
    st_mini_sleeping()
    st_mini_alert()
    st_mini_happy()
    st_mini_enter()
    st_yawning()
    st_dozing()
    st_collapsing()
    st_waking()
    print("done →", OUT)
