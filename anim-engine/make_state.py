# -*- coding: utf-8 -*-
"""
make_state.py — 桌宠动画一条龙：状态名 → 透明循环 GIF

整条管线：
  ① Qwen-Image-2.1 文生图（官方 RGBA 触发语，直接出透明关键帧）
  ② Pillow 合成绿底（H3 只吃 RGB；绿幕是抠图锚点）
  ③ MiniMax H3 图生视频（--first-frame = --last-frame ⇒ 无缝循环）
  ④ pet_anim_pipeline.py 抠绿 + 缩放 + 抽帧 → 320x320 / 10fps GIF

用法：
  python make_state.py --state idle --action "gently bobs up and down as if floating in water"
  python make_state.py --state happy --action "bounces joyfully, eyes sparkle" --keyframe existing.png
  python make_state.py --state idle --dry-run      # 只打印命令不执行

参数：
  --keyframe   复用已有透明关键帧（跳过 ①）
  --seconds    视频时长（默认 5.2 → 124 帧）
  --theme-dir  输出目录（默认 themes/zero/assets）
"""
import argparse
import os
import subprocess
import sys

QWEN_DIR = r"C:\Users\zcxzx\.workbuddy\skills\qwen-image-21"
H3_DIR = r"C:\Users\zcxzx\.workbuddy\skills\minimax-h3-video"
ENGINE = os.path.dirname(os.path.abspath(__file__))
COMFY_OUT = r"C:\Users\zcxzx\Documents\ComfyUI\output"

# 「零」的角色设定（所有状态共用，保证一致性）
CHARACTER = (
    "This is an RGBA image with transparency. "
    "一只可爱的漂浮小生物，TV动画风格，赛璐璐平涂上色。圆润的水滴形身体，雪白色，"
    "带淡淡的青色光晕。一只大而圆的发光眼睛，青蓝色瞳孔。头顶悬浮一枚小小的六角形冰晶。"
    "身体下方有柔和的青色尾迹光。"
)
TRANSPARENT_TAIL = (
    "The image has alpha channel and the background is transparent. "
    "角色居中，占画面约三分之二。干净的动画描线。无文字，无水印。"
)

# 状态 → 动作描述（I2V 只写动作，画面由关键帧决定）
ACTIONS = {
    "idle":      "gently bobs up and down as if floating in water; the ice crystal above its head slowly rotates; calm and subtle",
    "happy":     "bounces up and down joyfully, spins once in place, eyes sparkle brightly; the ice crystal flashes with light",
    "thinking":  "tilts slightly and slowly circles in place, one small fin rests under the chin as if pondering; the ice crystal pulses gently",
    "working":   "small focused movements, typing-like fin taps and slight forward leans, occasionally nudges an invisible keyboard; the ice crystal blinks steadily",
    "sleeping":  "sinks down slowly, eyes closed, body gently sways as if breathing; the ice crystal dims and glows softly in rhythm",
}

GREEN = (0, 255, 0)


def run(cmd, cwd=None, env_extra=None):
    print("+ " + " ".join(cmd))
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    r = subprocess.run(cmd, cwd=cwd, env=env)
    if r.returncode != 0:
        sys.exit(f"step failed: {' '.join(cmd)}")


def synth_green(src, dst):
    from PIL import Image
    im = Image.open(src).convert("RGBA")
    bg = Image.new("RGB", im.size, GREEN)
    bg.paste(im, mask=im.split()[3])
    bg.save(dst)
    return dst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True, help="状态名，如 idle / happy / thinking / working / sleeping")
    ap.add_argument("--action", default=None, help="覆盖该状态的动作描述（英文，只写动作）")
    ap.add_argument("--keyframe", default=None, help="复用已有透明关键帧 PNG（跳过生图）")
    ap.add_argument("--emotion", default=None, help="复用同一关键帧只换动作时，等于 --keyframe")
    ap.add_argument("--seconds", type=float, default=5.2)
    ap.add_argument("--size", type=int, default=320)
    ap.add_argument("--fps", type=int, default=10)
    ap.add_argument("--theme-dir", default=os.path.join(ENGINE, "..", "clawd-on-desk", "themes", "zero", "assets"))
    ap.add_argument("--work-dir", default=os.path.join(ENGINE, "work"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    os.makedirs(a.work_dir, exist_ok=True)
    os.makedirs(a.theme_dir, exist_ok=True)

    action = a.action or ACTIONS.get(a.state)
    if not action:
        sys.exit("unknown state: " + a.state + "  (pass --action to define it)")

    kf = a.keyframe
    if kf:
        kf = os.path.abspath(kf)
        if not os.path.isfile(kf):
            sys.exit("keyframe not found: " + kf)
    else:
        kf_png = os.path.join(COMFY_OUT, f"Zero_{a.state}_kf.png")
        # 关键帧 = 角色设定 + 该状态的表情姿势（中文描述）+ 透明尾缀
        pose_zh = {
            "idle": "表情平静友好，微微漂浮的姿态。",
            "happy": "开心大笑的表情，眼睛弯成月牙，身体上扬。",
            "thinking": "一只小鳍托着下巴，眼睛向上看，若有所思。",
            "working": "认真的表情，两只小鳍向前伸出像在敲键盘。",
            "sleeping": "闭着眼睛，身体放松下垂，安静漂浮。",
        }.get(a.state, "表情平静友好。")
        prompt = f"{CHARACTER} {pose_zh} {TRANSPARENT_TAIL}"
        cmd = ["python", "scripts\\qwen_gen.py", "-p", prompt,
               "--w", "576", "--h", "576", "--prefix", f"Zero_{a.state}_kf"]
        if a.dry_run:
            print("DRY ①:", " ".join(cmd), "(cwd=%s)" % QWEN_DIR)
        else:
            run(cmd, cwd=QWEN_DIR)
        # qwen_gen 的产物名带序号，取 output 里该前缀最新的
        cands = [f for f in os.listdir(COMFY_OUT) if f.startswith(f"Zero_{a.state}_kf") and f.endswith(".png")]
        if not cands:
            sys.exit("keyframe not produced")
        kf = os.path.join(COMFY_OUT, sorted(cands)[-1])
        print("keyframe:", kf)

    green = os.path.join(a.work_dir, f"Zero_{a.state}_green.png")
    if a.dry_run:
        print("DRY ②: synth green ->", green)
    else:
        synth_green(kf, green)

    video = os.path.join(COMFY_OUT, "video", f"H3_none_124f_{a.state}.mp4")
    h3_cmd = ["python", "scripts\\h3_gen.py", "-p",
              ("The small white creature " + action +
               ". The camera stays completely fixed with no zoom and no pan. "
               "The background remains exactly the same solid pure green with no changes, "
               "no shadows, no gradients. Seamless loop motion, subtle and calm."),
              "--first-frame", green, "--last-frame", green,
              "--w", "576", "--h", "576", "--seconds", str(a.seconds), "--fasth3"]
    if a.dry_run:
        print("DRY ③:", " ".join(h3_cmd), "(cwd=%s)" % H3_DIR)
    else:
        run(h3_cmd, cwd=H3_DIR)

    # H3 产物名带序号，找 video/ 下最新的该前缀文件
    vids = [f for f in os.listdir(os.path.join(COMFY_OUT, "video"))
            if f.startswith("H3_none_124f") and f.endswith(".mp4")]
    if not vids:
        sys.exit("video not produced")
    video = os.path.join(COMFY_OUT, "video", sorted(vids)[-1])

    out_gif = os.path.join(a.theme_dir, f"zero-{a.state}.gif")
    pipe_cmd = ["python", os.path.join(ENGINE, "pet_anim_pipeline.py"),
                "--video", video, "--out", out_gif,
                "--size", str(a.size), "--fps", str(a.fps), "--hue-range", "22"]
    if a.dry_run:
        print("DRY ④:", " ".join(pipe_cmd))
    else:
        run(pipe_cmd)

    print("DONE:", out_gif)


if __name__ == "__main__":
    main()
