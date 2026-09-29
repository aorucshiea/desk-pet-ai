"""organ_resonance — 嵌入共振器官。

想起旧事不是随机的，是共振：当前这句话和过去某个事件的语义越近，
那段记忆就越容易被点亮。本器官负责 find_resonance（找共振）和
build_resonance_block（把共振结果写成注入块），并在记忆被改动时
mark_stale（标记缓存失效）。

它被三处调用（说话前、思考时、记忆更新后），所以迁移用的是
"服务 + 降级"而不是搬代码：停用它 → 降级回内置 resonance.py。
"""

inject = ["events"]


def apply(ctx):
    from gateway.memory import resonance as _resonance

    ctx.provide("resonance", _resonance)
    ctx.log("resonance organ online")
