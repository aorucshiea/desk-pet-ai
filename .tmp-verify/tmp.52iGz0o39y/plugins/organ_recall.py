"""organ_recall — 主动回忆器官（模型自己决定"想起点什么"）。

这是桌宠唯一一个**模型主动调用**的记忆能力：模型觉得需要旧事时，自己
调用这个工具去检索。它原本经 mcp_manager.register_builtin() 硬编码注册，
现在是可插拔器官。

迁移方式与前面几个不同：这是**工具**，所以插件用 ctx.tool() 注册，
server.py 只在器官缺席时才兜底注册内置版本 —— 于是"停用器官"对模型是
真的失去这个能力，而不是换了个实现偷偷继续工作。

schema 原样取自 gateway.memory.recall.RECALL_TOOL_SCHEMA，零字段漂移。
"""

inject = ["events"]


def apply(ctx):
    from gateway.memory import recall as _recall
    from gateway.memory.recall import RECALL_TOOL_SCHEMA as sch

    # 模型可调用工具：名字/描述/参数完全沿用原 schema
    ctx.tool(
        sch["name"],
        sch["description"],
        sch["input_schema"],
        _recall.recall_tool_handler,
    )
    # 顺带把模块暴露成服务（session recall count 走这里，可降级）
    ctx.provide("recall", _recall)
    ctx.log("recall organ online (tool=%s)" % sch["name"])
