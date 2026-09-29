"""organ_continuity — 自我连续器官（持续自我存在）。

它是"同一个我"的那根线：把上一次的自我注释（renote）带进这一轮，
让桌宠不是每轮都从零开始，而是接着上一次的自己继续。

纯函数式助手（读一个当前注释），无依赖 —— 所以不 inject，永不停靠。
停用它 = 降级回 gateway/memory/continuity.py。
"""

inject = []


def apply(ctx):
    from gateway.memory import continuity as _continuity

    ctx.provide("continuity", _continuity)
    ctx.log("continuity organ online")
