"""mood_watch — 注入真实服务的范本（这就是"潜意识"最朴素的形态）。

它 inject 了 mood / events 两个服务：内核会在这些服务就绪时才把它唤醒
（服务没来就 parked 等着，来了自动激活）。它不打扰你，但一直在看。

想验证 coeffect：把 inject 里的 "mood" 删掉再存盘 —— 5 秒后它会以
"等待服务" 的状态停在 pending 列表里，而不是报错。
"""

inject = ["mood", "events"]

config = {
    "trace_limit": {"type": "int", "default": 20, "description": "最多保留几条情绪轨迹"},
}


def apply(ctx):
    trace = ctx.state.setdefault("trace", [])
    limit = ctx.config.get("trace_limit", 20)

    def mood_now(args):
        mood = ctx.mood                      # 注入的服务（可能为 None）
        idx = getattr(mood, "emotion_index", None) if mood is not None else None
        recent = "、".join(str(t) for t in trace[-5:]) or "（还没有记录）"
        return "当前情绪指数：%s｜轨迹 %d 条｜最近：%s" % (idx, len(trace), recent)

    ctx.tool(
        "pet_mood_now",
        "读当前的情绪指数与最近的情绪轨迹",
        {"type": "object", "properties": {}},
        mood_now,
    )

    def on_mood(**payload):
        trace.append(payload.get("emotion", "?"))
        if len(trace) > limit:
            del trace[:-limit]

    ctx.on("mood_changed", on_mood)
    ctx.log("mood_watch armed (limit=%d)" % limit)
