"""Dream cycle (梦境固化) tests — the autonomous memory consolidation
subsystem: time gates, candidate selection, prompt construction, and
the apply path (merge fragments → distilled event, promote core, record
the dream itself).
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

from gateway.memory import dream
from gateway.memory.events import EventStore

_dir_counter = itertools.count()


def _store(tmp_path, *events: dict) -> EventStore:
    # One fresh directory per store — EventStore persists to disk, so a
    # shared dir would leak events between stores.
    d = tmp_path / f"mem-{next(_dir_counter)}"
    d.mkdir(exist_ok=True)
    store = EventStore()
    store.load_from_disk(d)
    for kwargs in events:
        kwargs = dict(kwargs)
        pause = kwargs.pop("pause_decay", None)
        evt = store.add_event(**kwargs)
        if pause is not None:
            evt["pause_decay"] = bool(pause)
    store.save()
    return store


def _evt(title, content, weight, **kw):
    return {"title": title, "content": content, "weight": weight, **kw}


# ── should_dream gates ───────────────────────────────────────────────

def test_should_dream_gates(tmp_path):
    store = _store(
        tmp_path,
        _evt("a", "第一段碎片（平淡）", 100),
        _evt("b", "第二段碎片（平淡）", 120),
        _evt("c", "第三段碎片（平淡）", 140),
        _evt("d", "第四段碎片（平淡）", 160),
    )
    now = datetime.now(timezone.utc)

    # Never talked → no dream.
    assert dream.should_dream(store, None, now=now)[0] is False

    # Recent conversation → no dream (not idle enough).
    recent = now - timedelta(hours=1)
    assert dream.should_dream(store, recent, now=now)[0] is False

    # Idle long enough, enough fragments → due.
    idle = now - timedelta(hours=dream.IDLE_HOURS + 1)
    ok, reason = dream.should_dream(store, idle, now=now)
    assert ok and reason == "due"

    # Cooldown: dreamed recently → blocked even when idle.
    state = {"last_dream_at": (now - timedelta(hours=2)).isoformat()}
    assert dream.should_dream(store, idle, state=state, now=now)[0] is False

    # Cooldown expired → due again.
    state = {"last_dream_at": (now - timedelta(hours=dream.COOLDOWN_HOURS + 1)).isoformat()}
    assert dream.should_dream(store, idle, state=state, now=now)[0] is True

    # Too few fragments → blocked even when idle.
    tiny = _store(tmp_path, _evt("a", "碎片（平淡）", 100))
    assert dream.should_dream(tiny, idle, now=now)[0] is False


# ── candidate selection ──────────────────────────────────────────────

def test_collect_candidates_filters_and_sorts(tmp_path):
    store = _store(
        tmp_path,
        _evt("faint", "最模糊（平淡）", 40),
        _evt("ghost", "更模糊（平淡）", 10),
        _evt("vivid", "清晰（开心）", 500),
        _evt("held", "正在想（紧张）", 100, pause_decay=True),
        _evt("precious", "核心（开心）", 100, core=True),
        _evt("mid", "中档（平淡）", 150),
    )
    cands = dream.collect_candidates(store)
    ids = [e["title"] for e in cands]
    assert ids == ["ghost", "faint", "mid"]  # faintest first; vivid/held/precious excluded
    assert len(cands) <= dream.MAX_CANDIDATES


def test_collect_candidates_caps(tmp_path):
    events = [_evt(f"e{i}", f"碎片{i}（平淡）", 50 + i) for i in range(30)]
    store = _store(tmp_path, *events)
    assert len(dream.collect_candidates(store)) == dream.MAX_CANDIDATES


# ── prompt construction ──────────────────────────────────────────────

def test_build_dream_prompt_contains_fragments(tmp_path):
    store = _store(
        tmp_path,
        _evt("加班", "加班到凌晨（疲惫）", 150),
        _evt("猫", "楼下的猫（开心）", 80),
    )
    cands = dream.collect_candidates(store)
    system, user = dream.build_dream_prompt(cands, mood_ctx="【你此刻的心情】平静")
    assert "<<<MEM>>>" in system and "dreams" in system
    assert "编造" in system  # no-invention rule present
    assert "evt_" in user and "加班" in user and "猫" in user
    assert "平静" in system  # mood injected


# ── apply path ───────────────────────────────────────────────────────

def test_apply_dream_merges_fragments(tmp_path):
    store = _store(
        tmp_path,
        _evt("加班", "加班到凌晨（疲惫）", 150),
        _evt("周报", "周报写不完（疲惫）", 120),
        _evt("猫", "楼下的猫（开心）", 80),
    )
    e1, e2, e3 = store.get_all_events()
    data = {
        "dreams": [
            {
                "title": "那些熬过的夜",
                "content": "加班到凌晨（疲惫）。周报总也写不完（疲惫）。",
                "weight": 300,
                "merge_ids": [e1["id"], e2["id"]],
            }
        ],
        "core": [],
        "dream_note": "梦里都在赶工（疲惫）。",
    }
    outcome = dream.apply_dream(store, data)

    assert outcome["dreams_applied"] == 1
    assert outcome["fragments_merged"] == 2
    # Merged originals removed, distilled event + dream event present.
    titles = [e["title"] for e in store.get_all_events()]
    assert "那些熬过的夜" in titles and "梦" in titles
    assert "加班" not in titles and "周报" not in titles
    # The cat fragment (not merged) survives.
    assert "猫" in titles
    # Dream-born weight capped at 600, fragments' max was 150 → 150.
    merged = next(e for e in store.get_all_events() if e["title"] == "那些熬过的夜")
    assert merged["weight"] == 150


def test_apply_dream_never_dissolves_core_or_double_merges(tmp_path):
    store = _store(
        tmp_path,
        _evt("核心", "珍贵（开心）", 150, core=True),
        _evt("碎片", "会被梦到（平淡）", 100),
    )
    core_evt, frag = store.get_all_events()
    data = {
        "dreams": [
            {"title": "梦一", "content": "试图吞掉核心（疑惑）。",
             "weight": 300, "merge_ids": [core_evt["id"], frag["id"]]},
            {"title": "梦二", "content": "再次引用同一碎片（平淡）。",
             "weight": 300, "merge_ids": [frag["id"]]},
        ],
    }
    dream.apply_dream(store, data)
    # Core fragment untouched; the plain fragment merged exactly once.
    assert store.get_event(core_evt["id"]) is not None
    assert store.get_event(frag["id"]) is None
    titles = [e["title"] for e in store.get_all_events()]
    assert "梦一" in titles and "梦二" not in titles


def test_apply_dream_invalid_and_reflection(tmp_path):
    store = _store(tmp_path, _evt("碎片", "内容（平淡）", 100))
    frag = store.get_all_events()[0]

    # merge_ids pointing at nothing → dream skipped entirely.
    outcome = dream.apply_dream(store, {
        "dreams": [{"title": "空梦", "content": "没有来源（疑惑）。",
                    "weight": 300, "merge_ids": ["evt_missing_001"]}],
    })
    assert outcome["dreams_applied"] == 0
    assert "空梦" not in [e["title"] for e in store.get_all_events()]

    # Reflection dream without merge_ids → kept, lighter than lived.
    dream.apply_dream(store, {
        "dreams": [{"title": "感悟", "content": "梦里的想法（平静）。",
                    "weight": 300, "merge_ids": []}],
    })
    refl = next(e for e in store.get_all_events() if e["title"] == "感悟")
    assert refl["weight"] <= 400

    # The original fragment was never touched by these.
    assert store.get_event(frag["id"]) is not None


def test_apply_dream_promotes_core(tmp_path):
    store = _store(
        tmp_path,
        _evt("感悟碎片", "一个长久的感悟（平静）", 150),
        _evt("其他", "普通碎片（平淡）", 100),
    )
    outcome = dream.apply_dream(store, {"dreams": [], "core": ["感悟碎片"]})
    assert outcome["core_promoted"] == 1
    assert next(e for e in store.get_all_events() if e["title"] == "感悟碎片")["core"] is True
