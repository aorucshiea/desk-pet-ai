# -*- coding: utf-8 -*-
"""
pet_anim_pipeline.py — 桌宠动画后期管线（动画引擎核心件）

输入：H3 生成的绿幕视频（mp4，任意分辨率/帧率）
输出：桌宠可用的透明循环 GIF（对齐 cybercat 主题规格）

用法：
  python pet_anim_pipeline.py --video idle.mp4 --out idle.gif
  python pet_anim_pipeline.py --video idle.mp4 --out idle.gif --size 320 --fps 10 --max-frames 50 --key 60

流程：
  ① cv2 逐帧读视频
  ② 色键抠绿（HSV 范围 + 形态学清理 + 边缘羽化 + 去绿 spill）
  ③ 缩放到 --size（默认 320，桌宠标准）
  ④ 均匀抽帧到 --max-frames（桌宠 GIF 是 12-51 帧）
  ⑤ Pillow 合成 GIF（100ms/帧，loop=0 无限循环，disposal=2）
"""
import argparse
import os
import sys

import cv2
import numpy as np
from PIL import Image


def key_green(frame_bgr, key_hue=60, hue_range=28, s_min=55, v_min=40):
    """色键抠绿。返回 8-bit alpha（255=前景）。"""
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    green = (np.abs(h.astype(int) - key_hue) <= hue_range) & (s >= s_min) & (v >= v_min)
    alpha = np.where(green, 0, 255).astype(np.uint8)

    # 清理：开运算去掉背景噪点，闭运算补前景小洞
    k3 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    k7 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    alpha = cv2.morphologyEx(alpha, cv2.MORPH_OPEN, k3)
    alpha = cv2.morphologyEx(alpha, cv2.MORPH_CLOSE, k7)

    # 只对主体做最大连通域保留，去零星误判块
    n, labels, stats, _ = cv2.connectedComponentsWithStats(alpha, 8)
    if n > 2:
        areas = stats[1:, cv2.CC_STAT_AREA]
        keep = 1 + int(np.argmax(areas))
        mask = np.isin(labels, [0, keep])
        alpha = np.where(mask, alpha, 0).astype(np.uint8)

    # 边缘羽化
    alpha = cv2.GaussianBlur(alpha, (5, 5), 0)
    return alpha


def despill(frame_bgr, alpha):
    """去绿 spill：边缘半透明像素的 G 通道压到 max(R,B)。"""
    b, g, r = frame_bgr[:, :, 0].astype(int), frame_bgr[:, :, 1].astype(int), frame_bgr[:, :, 2].astype(int)
    limit = np.maximum(r, b)
    spill = (g > limit) & (alpha > 0)
    g2 = g.copy()
    g2[spill] = limit[spill]
    out = frame_bgr.copy()
    out[:, :, 1] = g2.astype(np.uint8)
    return out


def load_frames(video, max_frames=None):
    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        raise SystemExit("cannot open video: " + video)
    frames = []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        frames.append(f)
    cap.release()
    if not frames:
        raise SystemExit("no frames decoded: " + video)
    if max_frames and len(frames) > max_frames:
        idx = np.linspace(0, len(frames) - 1, max_frames).round().astype(int)
        idx = sorted(set(idx))
        frames = [frames[i] for i in idx]
    return frames


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--size", type=int, default=320, help="GIF 边长（桌宠标准 320）")
    ap.add_argument("--fps", type=int, default=10, help="GIF 帧率（桌宠标准 10）")
    ap.add_argument("--max-frames", type=int, default=50)
    ap.add_argument("--key", type=int, default=60, help="绿色的 OpenCV H 值（默认 60）")
    ap.add_argument("--hue-range", type=int, default=28)
    ap.add_argument("--no-despill", action="store_true")
    a = ap.parse_args()

    frames = load_frames(a.video, a.max_frames)
    duration = max(1, round(1000 / a.fps))

    pal_frames = []
    for i, f in enumerate(frames):
        alpha = key_green(f, key_hue=a.key, hue_range=a.hue_range)
        if not a.no_despill:
            f = despill(f, alpha)
        rgba = cv2.cvtColor(f, cv2.COLOR_BGR2BGRA)
        rgba[:, :, 3] = alpha
        rgb = cv2.cvtColor(rgba, cv2.COLOR_BGRA2RGBA)
        pil = Image.fromarray(rgb, "RGBA")
        if pil.size != (a.size, a.size):
            pil = pil.resize((a.size, a.size), Image.LANCZOS)
        # RGBA -> P（255 号索引作透明）
        alpha = pil.split()[3]
        p = pil.convert("RGB").quantize(colors=255, method=Image.FASTOCTREE)
        mask = Image.eval(alpha, lambda v: 255 if v <= 96 else 0)
        p.paste(255, mask)
        pal_frames.append(p)

    pal_frames[0].save(
        a.out, save_all=True, append_images=pal_frames[1:],
        duration=duration, loop=0, disposal=2, transparency=255, optimize=True,
    )
    kb = os.path.getsize(a.out) / 1024
    print(f"OK {a.out}  {len(pal_frames)} frames @ {a.fps}fps  {a.size}x{a.size}  {kb:.0f} KB")


if __name__ == "__main__":
    main()
