"""LingLing embedding resonance — semantic memory triggering.

When the user sends a message, this module checks if any stored event
semantically resonates with it. "好累" can hit "加班到凌晨两点" — not
through keyword matching, but through vector similarity.

Uses sentence-transformers locally (all-MiniLM-L6-v2, ~50ms inference).
Falls back to keyword matching if sentence-transformers is unavailable.

The resonance result is injected into the system prompt BEFORE the
model generates its reply, so the pet can say "I remember when you
stayed up until 2am" without the user explicitly asking about it.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from ..log_setup import get_logger
from .decay import on_event_accessed
from .events import EventStore
from . import loader

logger = get_logger()

# Lazy-loaded embedding model. Kept at module level so it persists
# across calls (model loading is expensive, inference is cheap).
_embedding_model = None
_event_embeddings: Dict[str, Any] = {}
_event_embeddings_stale = True


def _get_embedding_model():
    """Lazy-load sentence-transformers. Returns None if unavailable."""
    global _embedding_model
    if _embedding_model is not None:
        return _embedding_model
    try:
        from sentence_transformers import SentenceTransformer
        _embedding_model = SentenceTransformer("all-MiniLM-L6-v2")
        logger.info("Embedding model loaded: all-MiniLM-L6-v2")
        return _embedding_model
    except ImportError:
        logger.info("sentence-transformers not installed, using keyword fallback")
        return None
    except Exception as exc:
        logger.warning("Failed to load embedding model: %s", exc)
        return None


def _encode(text: str) -> Optional[Any]:
    """Encode a text string into an embedding vector."""
    model = _get_embedding_model()
    if model is None:
        return None
    try:
        return model.encode(text)
    except Exception as exc:
        logger.warning("Embedding encode failed: %s", exc)
        return None


def _cosine_similarity(a: Any, b: Any) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _rebuild_index(store: EventStore) -> None:
    """Rebuild the embedding index from all stored events.

    Called lazily when the index is stale (events added/removed).
    """
    global _event_embeddings, _event_embeddings_stale
    model = _get_embedding_model()
    if model is None:
        return

    _event_embeddings = {}
    for evt in store.get_all_events():
        if evt["weight"] <= 0:
            continue
        text = f"{evt.get('title', '')} {evt.get('content', '')}"
        emb = _encode(text)
        if emb is not None:
            _event_embeddings[evt["id"]] = emb

    _event_embeddings_stale = False
    logger.debug("Resonance index rebuilt: %d events", len(_event_embeddings))


def mark_stale() -> None:
    """Mark the embedding index as stale. Call after events are added/removed."""
    global _event_embeddings_stale
    _event_embeddings_stale = True


def find_resonance(
    store: EventStore,
    user_message: str,
    threshold: float = 0.65,
    top_k: int = 2,
) -> List[Dict[str, Any]]:
    """Find events that semantically resonate with the user's message.

    Args:
        store: The EventStore instance.
        user_message: The user's latest message text.
        threshold: Minimum cosine similarity to trigger (0.0-1.0).
        top_k: Maximum number of resonant events to return.

    Returns:
        List of resonant event dicts, sorted by similarity (highest first).
    """
    if not user_message or not user_message.strip():
        return []

    # Try embedding-based resonance first
    model = _get_embedding_model()
    if model is not None:
        if _event_embeddings_stale:
            _rebuild_index(store)

        msg_emb = _encode(user_message)
        if msg_emb is not None:
            hits: List[Tuple[str, float]] = []
            for eid, eemb in _event_embeddings.items():
                # Skip events already loaded in this session
                if loader.is_in_session(eid):
                    continue
                sim = _cosine_similarity(msg_emb, eemb)
                if sim >= threshold:
                    hits.append((eid, sim))

            hits.sort(key=lambda x: x[1], reverse=True)

            results = []
            for eid, sim in hits[:top_k]:
                evt = store.get_event(eid)
                if evt:
                    on_event_accessed(store, eid)
                    loader.add_to_session(eid)
                    results.append(evt)

            if results:
                logger.info(
                    "Resonance: %d events triggered by %r",
                    len(results), user_message[:50],
                )
            return results

    # Keyword fallback
    keywords = user_message.strip().lower().split()
    if not keywords:
        return []

    # Use the longest word as primary keyword (more specific)
    keyword = max(keywords, key=len)
    results = store.search_by_keyword(keyword, limit=top_k)

    # Filter out already-loaded events
    filtered = [e for e in results if not loader.is_in_session(e["id"])]

    for evt in filtered:
        on_event_accessed(store, evt["id"])
        loader.add_to_session(evt["id"])

    return filtered
