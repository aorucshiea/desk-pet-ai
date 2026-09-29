"""organ_impulse — 说话冲动器官（桌宠的潜意识兜底）。

模型每轮本该在回复末尾给出 [NEXT_CHAT:秒数]——那是意识层在决定
"我什么时候想再说话"。但它经常忘记，于是"我决定不说"退化成了
"我忘了决定"，桌宠从此永远沉默。

这个器官就是那个兜底：模型没给时间时，按此刻的状态算一个说话间隙。
它原本硬编码在 server.py + memory/impulse.py；现在这里**就是它的性格**
——下面 config 里的每个数字都会出现在设置页的插件配置编辑器里，
改完存盘 5 秒热加载，或者直接改这里。

设计边界（不要改坏）：
- 模型显式选择沉默（[NEXT_CHAT:0]）永不兜底，永远原样转发；
- 系统只决定"什么时候开口"，不决定"必须说什么"。
"""

# 纯函数器官：不依赖任何服务，所以它永不 park，随时可被聊天流调用。
inject = []

config = {
    "base_gap_seconds": {"type": "int", "default": 1200, "description": "基准说话间隙（秒）"},
    "min_gap_seconds": {"type": "int", "default": 180, "description": "最小间隙——绝不纠缠"},
    "max_gap_seconds": {"type": "int", "default": 10800, "description": "最大间隙——绝不消失"},
    "mood_happy_scale": {"type": "float", "default": 0.35, "description": "心情好时缩短比例（每 1.0 情绪指数）"},
    "mood_sad_scale": {"type": "float", "default": 0.5, "description": "心情差时拉长比例（不对称：低落需要更久）"},
    "backoff_factor": {"type": "float", "default": 1.6, "description": "连续冷场的退避倍率"},
    "backoff_cap": {"type": "int", "default": 5, "description": "退避最多叠几次"},
    "recall_ease": {"type": "float", "default": 0.04, "description": "每次成功回忆的缩短比例"},
    "recall_ease_cap": {"type": "int", "default": 5, "description": "回忆加成最多叠几次"},
}


def apply(ctx):
    cfg = ctx.config

    def compute_gap(emotion_index=0.0, proactive_streak=0, recall_count=0):
        """距离下一次主动开口还有几秒。纯函数：无 I/O、无随机。"""
        idx = max(-1.0, min(1.0, emotion_index or 0.0))
        gap = float(cfg.get("base_gap_seconds", 1200))

        if idx > 0:
            gap *= 1.0 - float(cfg.get("mood_happy_scale", 0.35)) * idx
        elif idx < 0:
            gap *= 1.0 + float(cfg.get("mood_sad_scale", 0.5)) * (-idx)

        streak = max(0, min(int(cfg.get("backoff_cap", 5)), int(proactive_streak or 0)))
        gap *= float(cfg.get("backoff_factor", 1.6)) ** streak

        rec = max(0, min(int(cfg.get("recall_ease_cap", 5)), int(recall_count or 0)))
        gap *= 1.0 - float(cfg.get("recall_ease", 0.04)) * rec

        lo = int(cfg.get("min_gap_seconds", 180))
        hi = int(cfg.get("max_gap_seconds", 10800))
        return int(max(lo, min(hi, round(gap))))

    # 暴露给聊天流；器官被停用时 server.py 降级回内置 memory/impulse.py。
    # 服务必须是"带属性的对象"而不是 dict：调用点是 svc.compute_gap(...)，
    # 而降级目标（内置模块）就是属性形状，两者必须一致。
    from types import SimpleNamespace
    ctx.provide("impulse", SimpleNamespace(compute_gap=compute_gap))
    ctx.log("speech impulse armed (base=%ss)" % cfg.get("base_gap_seconds"))
