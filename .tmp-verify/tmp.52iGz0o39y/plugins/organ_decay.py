"""organ_decay — 记忆衰减器官。

每 10 分钟跑一次 run_decay：正在被思考的事件（pause_decay=True）不动，
已固化的衰减更慢；几小时没人说话就冻结——记忆只在凌凌醒着的时候淡去。
对话结束的瞬间会额外做一次会话末衰减（内核事件 conversation_end）。

它原本是 server.py 里的硬编码循环；现在是可编辑的插件——改完存盘，
5 秒内热加载。inject 声明依赖：events 或 conversation_state 没就绪时
本器官会停靠等待，而不是报错。
"""

inject = ["events", "conversation_state"]

config = {
    "interval_seconds": {"type": "int", "default": 600, "description": "衰减巡检间隔（秒）"},
}


def apply(ctx):
    import threading

    from gateway.memory import decay as _decay

    interval = int(ctx.config.get("interval_seconds") or 600)
    stop = threading.Event()

    def _loop():
        while not stop.wait(interval):
            try:
                last = (ctx.require("conversation_state") or {}).get("last_conversation_at")
                _decay.run_decay(ctx.events, last)
            except Exception as exc:
                ctx.log("decay error: %s" % exc)

    thread = threading.Thread(target=_loop, name="organ_decay", daemon=True)
    thread.start()

    def on_conversation_end(**payload):
        try:
            _decay.on_conversation_end(ctx.events)
        except Exception as exc:
            ctx.log("conversation-end decay error: %s" % exc)

    ctx.on("conversation_end", on_conversation_end)
    ctx.effect("thread:organ_decay", stop.set, "memory decay ticker")
