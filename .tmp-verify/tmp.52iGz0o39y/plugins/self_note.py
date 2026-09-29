"""self_note — 示例插件：给自己写一张随时可读的便签。

这就是一个完整的插件：apply(ctx) 是入口，ctx.tool() 注册模型可调用的能力，
inject 声明依赖的服务，config 声明可调参数（内核会校验并补默认值）。
改完这个文件，5 秒内自动热加载。（万物皆插件 —— 你可以铸造更多这样的器官。）"""

inject = ["events"]

config = {
    "limit": {"type": "int", "default": 50, "description": "最多保留几条便签"},
}


def apply(ctx):
    notes = ctx.state.setdefault("notes", [])
    limit = ctx.config.get("limit", 50)

    def add_note(args):
        text = str(args.get("text", "")).strip()
        if not text:
            return "[self_note: need text]"
        notes.append(text)
        if len(notes) > limit:
            del notes[:-limit]
        ctx.log("note added: " + text[:40])
        return "记下了（共 %d 条）: %s" % (len(notes), text)

    def read_notes(args):
        if not notes:
            return "（便签是空的）"
        return "\n".join("- " + n for n in notes)

    ctx.tool(
        "pet_self_note_add",
        "给自己写一张便签（持久保存在本次进程内）",
        {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        add_note,
    )
    ctx.tool(
        "pet_self_note_read",
        "读自己的便签",
        {"type": "object", "properties": {}},
        read_notes,
    )
    ctx.on("theme_switched", lambda theme: notes.append("[换到了 %s 的身体]" % theme))
