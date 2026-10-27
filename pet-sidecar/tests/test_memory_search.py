from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from gateway import server as server_mod
from gateway.memory import search
from gateway.memory.events import EventStore
from gateway.memory.files import MemoryFiles


def _write_day(root: Path, day: str, *turns: tuple[str, str]) -> None:
    directory = root / "conversations"
    directory.mkdir(parents=True, exist_ok=True)
    records = [
        json.dumps({"role": role, "content": content}, ensure_ascii=False)
        for role, content in turns
    ]
    (directory / f"{day}.jsonl").write_text("\n".join(records) + "\n", encoding="utf-8")


def _seed(root: Path) -> EventStore:
    store = EventStore()
    store.load_from_disk(root)
    store.add_event("星河咖啡馆", "我们一起去星河咖啡馆（开心）", 500)
    MemoryFiles(root).write("经历/咖啡.md", "用户最喜欢星河咖啡馆的手冲。")
    _write_day(root, "2026-10-19", ("user", "明天还去星河咖啡馆吗"), ("assistant", "好啊"))
    return store


def test_searches_all_three_sources_with_provenance(tmp_path):
    store = _seed(tmp_path)
    results = search.search_memory("星河咖啡", store, tmp_path, limit=10)

    assert {item["source"] for item in results} == {"event", "memory_file", "conversation"}
    event = next(item for item in results if item["source"] == "event")
    memory_file = next(item for item in results if item["source"] == "memory_file")
    conversation = next(item for item in results if item["source"] == "conversation")
    assert event["event_id"].startswith("evt_") and event["snippet"]
    assert memory_file["path"] == "经历/咖啡.md" and memory_file["snippet"]
    assert conversation["day"] == "2026-10-19" and conversation["turn"] == 0
    assert all(item["score"] > 0 for item in results)


def test_event_ranking_uses_weight_and_recency(tmp_path):
    store = EventStore()
    store.load_from_disk(tmp_path)
    weak = store.add_event("加班记忆", "内容甲", 100)
    strong = store.add_event("加班记忆", "内容乙", 900)
    old = (datetime.now(timezone.utc) - timedelta(days=180)).isoformat()
    weak["created_at"] = old
    strong["created_at"] = old
    store.save()

    results = search.search_memory("加班记忆", store, tmp_path, scope="events")
    assert [item["event_id"] for item in results] == [strong["id"], weak["id"]]

    recent = store.add_event("新的加班记忆", "内容丙", 100)
    results = search.search_memory("加班记忆", store, tmp_path, scope="events", limit=3)
    assert results[0]["event_id"] == strong["id"]
    assert next(item for item in results if item["event_id"] == recent["id"])["score"] > next(
        item for item in results if item["event_id"] == weak["id"]
    )["score"]


def test_empty_limit_scope_and_english_words(tmp_path):
    store = _seed(tmp_path)
    assert search.search_memory("   ", store, tmp_path) == []
    assert len(search.search_memory("星河", store, tmp_path, limit=1)) == 1
    assert len(search.search_memory("星河", store, tmp_path, limit=999)) <= search.MAX_LIMIT
    assert {item["source"] for item in search.search_memory("星河", store, tmp_path, scope="files")} == {"memory_file"}
    assert {item["source"] for item in search.search_memory("星河", store, tmp_path, scope="logs")} == {"conversation"}
    assert search.search_memory("不存在", store, tmp_path, scope="events") == []

    store.add_event("Coffee plan", "We discussed coffee", 300)
    assert search.search_memory("coffee", store, tmp_path, scope="events")
    assert search.search_memory("coff", store, tmp_path, scope="events") == []


def test_file_search_stays_inside_memory_sandbox(tmp_path):
    store = EventStore()
    store.load_from_disk(tmp_path)
    secret = tmp_path / "secret.md"
    secret.write_text("绝密星河内容", encoding="utf-8")
    files = MemoryFiles(tmp_path / "nested")
    files.write("公开.md", "普通的公开记忆")

    assert search.search_memory("../secret", store, tmp_path / "nested") == []
    assert search.search_memory("绝密星河", store, tmp_path / "nested") == []
    assert secret.read_text(encoding="utf-8") == "绝密星河内容"


def test_http_search_uses_current_theme_store_and_directory(tmp_path, monkeypatch):
    root = tmp_path / "memories"
    monkeypatch.setenv("PET_MEMORY_DIR", str(root))
    app = server_mod.build_app(initial_model=None)
    with TestClient(app) as client:
        store = server_mod.get_event_store()
        event = store.add_event("本机搜索", "通过接口找到本机搜索（开心）", 400)
        MemoryFiles(root).write("接口.md", "本机搜索也覆盖文件")
        _write_day(root, "2026-10-18", ("user", "本机搜索日志"))
        response = client.post("/api/memory/search", json={"query": "本机搜索", "limit": 8})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True and body["count"] == 3
    assert {item["source"] for item in body["results"]} == {"event", "memory_file", "conversation"}
    assert any(item.get("event_id") == event["id"] for item in body["results"])


def test_log_reader_reports_bytes_and_respects_cap(tmp_path):
    _write_day(tmp_path, "2026-10-19", ("user", "一" * 50), ("assistant", "二" * 50))
    turns, used = search._read_log_bounded(tmp_path, "2026-10-19", max_bytes=40)
    assert used <= 40
    assert isinstance(turns, list)

    turns, used = search._read_log_bounded(tmp_path, "2026-10-19", max_bytes=0)
    assert turns == [] and used == 0


def test_log_search_prefers_newest_days_when_budget_is_tight(tmp_path, monkeypatch):
    _write_day(tmp_path, "2026-10-18", ("user", "旧日志 星河"))
    _write_day(tmp_path, "2026-10-19", ("user", "新日志 星河"))
    first = (tmp_path / "conversations" / "2026-10-19.jsonl").stat().st_size
    # Exactly enough for the newer file, none for the older one.
    monkeypatch.setattr(search, "MAX_LOG_TOTAL_BYTES", first)
    results = search.search_memory("星河", None, tmp_path, scope="logs")
    assert results and {item["day"] for item in results} == {"2026-10-19"}
