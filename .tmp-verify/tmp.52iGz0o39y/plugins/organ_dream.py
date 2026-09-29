"""organ_dream — 梦境固化器官。

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
