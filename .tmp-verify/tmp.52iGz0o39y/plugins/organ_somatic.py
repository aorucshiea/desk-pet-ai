"""organ_somatic — 身体感受器官（体感）。

桌宠的"身体"感：用户怎么对待它（拖拽、连点、长时间不理）会累积成
感觉，下一轮对话时以"身体感受"注入——它让被拖来拖去这件事真的留下
痕迹，而不只是一个动画。

⚠️ SensationBuffer 实例**留在 server.py**（模块级），因为它是有状态的：
重载器官不该清空已经积累的感受。本器官只提供 record /
build_somatic_block / EVENT_WEIGHT 这几项能力。

inject 了 events：感觉最终要记成事件。
"""

inject = ["events"]


def apply(ctx):
    from gateway.memory import somatic as _somatic

    ctx.provide("somatic", _somatic)
    ctx.log("somatic organ online")
