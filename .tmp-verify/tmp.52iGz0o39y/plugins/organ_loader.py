"""organ_loader — 记忆装载器官。

每一次说话之前，它决定"这次该想起什么"：把最近的、核心的、和当前
话题相关的事件装配成一段记忆上下文，注入给模型。原本硬编码在
server.py 的请求路径上（build_memory_context），现在是可插拔器官。

停用它：server.py 会降级回内置 gateway/memory/loader.py（聊天照常），
但它不再出现在 Evolve 页，也不能被改写。

inject 了 events —— 事件库没就绪时本器官停靠等待，而不是报错。
"""

inject = ["events"]


def apply(ctx):
    from gateway.memory import loader as _loader

    # 暴露给聊天流；provide 的副作用是可回滚的（卸载即消失）
    ctx.provide("loader", _loader)
    ctx.log("memory loader organ online")
