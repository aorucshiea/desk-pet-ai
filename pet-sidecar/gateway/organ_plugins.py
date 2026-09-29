"""Built-in organ plugins, shipped as seed sources.

The organs that used to be hardcoded loops inside server.py's build_app
live here as plugin sources. `PluginManager.seed_core_plugins` writes any
missing file into the live plugins dir (idempotent, so model edits are
never overwritten), and the hot loader brings them up like any other
plugin — editable, unloadable, visible in the effect ledger.
"""

ORGAN_SOURCES: dict[str, str] = {}

ORGAN_SOURCES["organ_decay.py"] = '''"""organ_decay — 记忆衰减器官。

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
'''

ORGAN_SOURCES["organ_dream.py"] = '''"""organ_dream — 梦境固化器官。

闲置足够久之后，网关自己跑一次固化：收集模糊的碎片，交给当前可用的
provider 蒸馏成结构化记忆，并把这一夜记成 dream 事件。记忆维护作为
结构化的潜意识器官——不是模型必须记得去做的一件杂务。

依赖 schedule（主循环任务提交）/ providers / llama_server / memory_ctx
/ conversation_state：内核会在这些服务就绪时才唤醒本器官。巡检间隔
可在 config 里调；改完存盘 5 秒热加载，任务会在主循环上被重启。
"""

inject = [
    "events",
    "mood",
    "memory_ctx",
    "providers",
    "llama_server",
    "schedule",
    "conversation_state",
]

config = {
    "check_interval_seconds": {"type": "int", "default": 1800, "description": "梦境巡检间隔（秒）"},
}


def apply(ctx):
    import asyncio
    from datetime import datetime, timezone

    from gateway.memory import dream as _dream
    from gateway.memory import resonance as _resonance
    from gateway.memory.events import parse_event_block

    interval = int(ctx.config.get("check_interval_seconds") or 1800)
    task_holder = {}

    async def _maybe_run_dream() -> str:
        mem_ctx = ctx.require("memory_ctx")
        events = ctx.events
        state = _dream.load_state(mem_ctx.theme_dir(mem_ctx.current_theme))
        due, reason = _dream.should_dream(
            events,
            (ctx.require("conversation_state") or {}).get("last_conversation_at"),
            state=state,
        )
        if not due:
            return ""

        # Provider pick: prefer a cloud provider (cheap, better at
        # instruction-following); fall back to the local model if it's
        # the only one and it's alive.
        providers = ctx.require("providers")
        provider = None
        for name, p in providers._providers.items():
            if name != "local":
                provider = p
                break
        if provider is None and getattr(ctx.require("llama_server"), "alive", False):
            provider = providers.get_or("local")
        if provider is None:
            ctx.log("dream deferred: no provider available")
            return ""

        candidates = _dream.collect_candidates(events)
        if len(candidates) < _dream.MIN_CANDIDATES:
            return ""
        mood_store = ctx.mood
        mood_ctx = mood_store.format_for_system_prompt() if mood_store else ""
        system, user = _dream.build_dream_prompt(candidates, mood_ctx)

        reply_chunks: list[str] = []
        async for ev in provider.chat(
            [{"role": "user", "content": user}],
            system=system,
            max_tokens=1024,
            temperature=0.4,
            top_p=0.95,
        ):
            if ev.get("type") == "delta":
                reply_chunks.append(ev.get("content", ""))
            elif ev.get("type") == "error":
                raise RuntimeError(ev.get("message", "provider error"))
        reply = "".join(reply_chunks)

        data = parse_event_block(reply) or {}
        outcome = _dream.apply_dream(events, data)
        _resonance.mark_stale()
        _dream.save_state(mem_ctx.theme_dir(mem_ctx.current_theme), {
            **state,
            "last_dream_at": datetime.now(timezone.utc).isoformat(),
            "dream_count": int(state.get("dream_count", 0)) + 1,
            "last_outcome": outcome,
        })
        return outcome.get("summary", "")

    async def _dream_loop():
        while True:
            try:
                await asyncio.sleep(interval)
                summary = await _maybe_run_dream()
                if summary:
                    ctx.log("dream cycle complete: %s" % summary)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                ctx.log("dream loop error: %s" % exc)

    # Runs on the main asyncio loop (provider clients are loop-bound);
    # schedule() is thread-safe, so this also works on a hot-reload.
    sched = ctx.require("schedule")
    task_holder["task"] = sched(_dream_loop())

    def _cancel():
        task = task_holder.get("task")
        if task is not None:
            task.get_loop().call_soon_threadsafe(task.cancel)

    ctx.effect("task:organ_dream", _cancel, "dream cycle on the main loop")
'''

ORGAN_SOURCES["organ_impulse.py"] = '''"""organ_impulse — 说话冲动器官（桌宠的潜意识兜底）。

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

    # 暴露给聊天流；器官被停用时 server.py 降级回内置 memory/impulse.py
    ctx.provide("impulse", {"compute_gap": compute_gap})
    ctx.log("speech impulse armed (base=%ss)" % cfg.get("base_gap_seconds"))
'''

# ── 记忆装载 / 自我连续 / 共振 / 体感：请求路径上的四个同步器官 ──────────
# 它们不是主动循环，而是"每次说话前被问一句"的助手。迁移方式是暴露成
# 内核服务（ctx.provide），server.py 调用点走 _organ(name, 内置模块)——
# 停用器官 = 降级回内置实现，聊天不崩，但它从 Evolve 页消失、不再可改。

ORGAN_SOURCES["organ_loader.py"] = '''"""organ_loader — 记忆装载器官。

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
'''

ORGAN_SOURCES["organ_continuity.py"] = '''"""organ_continuity — 自我连续器官（持续自我存在）。

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
'''

ORGAN_SOURCES["organ_resonance.py"] = '''"""organ_resonance — 嵌入共振器官。

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
'''

ORGAN_SOURCES["organ_somatic.py"] = '''"""organ_somatic — 身体感受器官（体感）。

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
'''
