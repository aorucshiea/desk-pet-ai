"""LingLing embedding resonance v2 — the subconscious layer.

When the user sends a message, this module checks if any stored event
semantically resonates with it. "好累" can hit "加班到凌晨两点" — not
through keyword matching, but through vector similarity.

v2 (2026-08-01):
  - Backend switched from local sentence-transformers (which pulls in
    torch — banned by the gateway's no-torch constraint) to the free
    SiliconFlow embedding API (BAAI/bge-m3). Vectors are cached locally
    in ``{memory_dir}/vectors.json``; only new events trigger API calls.
  - Fallback chain when no API key / network failure:
      1. embedding resonance (semantic)
      2. emotion resonance (user message mentions an emotion word →
         events tagged with that emotion, by weight)
      3. keyword search (legacy substring)
    Never raises — the pet just doesn't "remember" on that turn.
  - The injection block uses felt language + time annotation: the model
    must know how old the surfaced memory is (很久以前的自己), and the
    resonance must read like a memory surfacing, not a database hit.

The resonance result is injected into the system prompt BEFORE the
model generates its reply, so the pet can say "I remember when you
stayed up until 2am" without the user explicitly asking about it.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..log_setup import get_logger
from .decay import on_event_accessed
from .events import EventStore, EMOTION_VOCAB
from .recall import human_time_ago
from . import loader

logger = get_logger()

EMBEDDING_URL = "https://api.siliconflow.cn/v1/embeddings"
EMBEDDING_MODEL = "BAAI/bge-m3"
API_KEY_ENV = "MINICPM_EMBEDDING_API_KEY"

# Minimum cosine similarity to trigger (0.0-1.0).
RESONANCE_THRESHOLD = 0.65

# Maximum number of resonant events to return.
TOP_K = 2

# Lazy-loaded local vector cache: event_id -> embedding.
_vectors: Dict[str, List[float]] = {}
_vectors_path: Optional[Path] = None


def set_vector_store(memory_dir: Path) -> None:
    """Point the vector cache at {memory_dir}/vectors.json. Call once at
    startup (before any resonance call)."""
    global _vectors_path, _vectors
    _vectors_path = Path(memory_dir).expanduser() / "vectors.json"
    _vectors = {}
    try:
        if _vectors_path.exists():
            _vectors = json.loads(_vectors_path.read_text(encoding="utf-8"))
            logger.info("Resonance vector cache loaded: %d entries", len(_vectors))
    except Exception as exc:
        logger.warning("Vector cache load failed (fresh start): %s", exc)
        _vectors = {}


def _save_vectors() -> None:
    if _vectors_path is None:
        return
    try:
        _vectors_path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(_vectors, ensure_ascii=False)
        tmp = _vectors_path.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(_vectors_path)
    except Exception as exc:
        logger.warning("Vector cache save failed: %s", exc)


def mark_stale() -> None:
    """No-op for backward compat: the v2 index is incremental — new
    events get embedded lazily on the next resonance check. Kept so
    server.py's extract flow keeps working unchanged."""


def _api_key() -> Optional[str]:
    key = os.environ.get(API_KEY_ENV, "").strip()
    return key or None


async def _embed_text(text: str) -> Optional[List[float]]:
    """Embed one text via the SiliconFlow API. None on any failure."""
    key = _api_key()
    if key is None:
        return None
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                EMBEDDING_URL,
                headers={"Authorization": f"Bearer {key}"},
                json={"model": EMBEDDING_MODEL, "input": text},
            )
            resp.raise_for_status()
            data = resp.json()
            return [float(x) for x in data["data"][0]["embedding"]]
    except Exception as exc:
        logger.warning("Embedding API call failed (falling back): %s", exc)
        return None


async def _ensure_event_vectors(store: EventStore) -> None:
    """Embed only events missing from the cache (incremental)."""
    for evt in store.get_all_events():
        if evt.get("weight", 0) <= 0:
            continue
        eid = evt["id"]
        if eid in _vectors:
            continue
        emb = await _embed_text(f"{evt.get('title', '')} {evt.get('content', '')}")
        if emb:
            _vectors[eid] = emb
    _save_vectors()


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _emotion_fallback(store: EventStore, user_message: str) -> List[Dict[str, Any]]:
    """Emotion resonance: the message mentions an emotion word → surface
    events tagged with that emotion, ranked by weight."""
    mentioned = [w for w in EMOTION_VOCAB if w in user_message]
    if not mentioned:
        return []
    hits: List[Dict[str, Any]] = []
    for evt in store.get_all_events():
        if evt.get("weight", 0) <= 0:
            continue
        if evt.get("emotion") in mentioned and not loader.is_in_session(evt["id"]):
            hits.append(evt)
    hits.sort(key=lambda e: e["weight"], reverse=True)
    return hits[:TOP_K]


async def find_resonance(
    store: EventStore,
    user_message: str,
    threshold: float = RESONANCE_THRESHOLD,
    top_k: int = TOP_K,
) -> List[Dict[str, Any]]:
    """Find events that resonate with the user's message.

    Try embedding resonance first (semantic); on missing API key /
    network failure fall back to emotion resonance, then keyword search.
    Surfaced events consolidate and join the session (no flicker).

    Returns:
        List of resonant event dicts, most similar first.
    """
    if not user_message or not user_message.strip():
        return []

    # 1. Embedding resonance (primary path).
    if _api_key() is not None:
        await _ensure_event_vectors(store)
        msg_emb = await _embed_text(user_message)
        if msg_emb is not None:
            hits: List[Tuple[Dict[str, Any], float]] = []
            for eid, vec in _vectors.items():
                if loader.is_in_session(eid):
                    continue
                evt = store.get_event(eid)
                if evt is None or evt.get("weight", 0) <= 0:
                    continue
                sim = _cosine_similarity(msg_emb, vec)
                if sim >= threshold:
                    hits.append((evt, sim))
            hits.sort(key=lambda x: x[1], reverse=True)
            results = [evt for evt, _ in hits[:top_k]]
            for evt in results:
                on_event_accessed(store, evt["id"])
                loader.add_to_session(evt["id"])
            if results:
                logger.info(
                    "Resonance: %d event(s) triggered by %r",
                    len(results), user_message[:50],
                )
            return results

    # 2. Emotion resonance (no API key / API failed).
    emotion_hits = _emotion_fallback(store, user_message)
    if emotion_hits:
        for evt in emotion_hits:
            on_event_accessed(store, evt["id"])
            loader.add_to_session(evt["id"])
        return emotion_hits

    # 3. Keyword fallback (legacy substring).
    keywords = user_message.strip().lower().split()
    if not keywords:
        return []
    keyword = max(keywords, key=len)
    results = store.search_by_keyword(keyword, limit=top_k)
    filtered = [e for e in results if not loader.is_in_session(e["id"])]
    for evt in filtered:
        on_event_accessed(store, evt["id"])
        loader.add_to_session(evt["id"])
    return filtered


def build_resonance_block(hits: List[Dict[str, Any]]) -> str:
    """Felt-language + time-annotated injection block for the system
    prompt. The model must read it as memory surfacing, not data."""
    lines = ["\n\n【你突然觉得这和现在有关】"]
    for evt in hits:
        when = human_time_ago(evt.get("created_at", ""))
        lines.append(f"- {when}的事：[{evt['title']}] {evt['content']}")
    return "\n".join(lines)
