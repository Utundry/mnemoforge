from __future__ import annotations

import hashlib
import logging
import math
from typing import Any

from app.config import settings
from app.services.llm_gateway import get_cloud_gateway
from app.services.lmstudio_service import LMStudioService
from app.services.text_localization import normalize_text_for_display

logger = logging.getLogger(__name__)


def stable_semantic_vector(text: str) -> list[float]:
    seed = normalize_text_for_display(str(text or "")).strip() or "empty semantic record"
    vector: list[float] = []
    for index in range(settings.embedding_dimensions):
        digest = hashlib.blake2b(f"{index}\0{seed}".encode("utf-8"), digest_size=8).digest()
        raw = int.from_bytes(digest, "big", signed=False)
        vector.append((raw / ((1 << 64) - 1)) * 2.0 - 1.0)
    norm = math.sqrt(sum(item * item for item in vector)) or 1.0
    return [item / norm for item in vector]


def _valid_vector(vector: Any) -> bool:
    return isinstance(vector, list) and len(vector) == settings.embedding_dimensions


async def _cloud_semantic_embedding(text: str, *, purpose: str) -> tuple[list[float], dict[str, str]] | None:
    prompt = (
        "Create a compact semantic signature for retrieval indexing. "
        "Return only dense keywords and short phrases, no prose.\n\n"
        f"Purpose: {purpose}\n\n"
        f"{text[:6000]}"
    )
    try:
        signature = await get_cloud_gateway().generate(
            prompt,
            system="You create compact semantic signatures for retrieval indexing.",
            task_type="text_summarization",
            mode="economy",
            max_tokens=256,
            temperature=0.0,
            timeout=45.0,
            allow_local_fallback=False,
        )
    except Exception as exc:
        logger.warning("Cloud semantic embedding fallback failed for %s: %s", purpose, exc)
        return None

    signature = normalize_text_for_display(signature).strip()
    if not signature:
        return None
    return stable_semantic_vector(signature), {
        "embedding_provider": "cloud_semantic_hash",
        "embedding_fallback_from": "local_embeddings",
    }


async def embed_text(
    text: str,
    *,
    primary: Any | None = None,
    purpose: str = "memory",
    fallback_reason: str = "embedding_unavailable",
) -> tuple[list[float], dict[str, str]]:
    if primary is not None:
        try:
            vector = await primary.embed(text)
            if _valid_vector(vector):
                return vector, {"embedding_provider": "primary"}
            if isinstance(vector, list):
                logger.warning(
                    "Primary embedding provider returned %d dimensions for %s, expected %d",
                    len(vector),
                    purpose,
                    settings.embedding_dimensions,
                )
        except Exception as exc:
            logger.warning("Primary embedding provider failed for %s, trying LM Studio: %s", purpose, exc)

    lmstudio: LMStudioService | None = None
    try:
        lmstudio = LMStudioService()
        vector = await lmstudio.embed(text)
        if _valid_vector(vector):
            return vector, {"embedding_provider": "lmstudio", "embedding_fallback_from": "primary"}
        if vector:
            logger.warning(
                "LM Studio embedding returned %d dimensions for %s, expected %d",
                len(vector),
                purpose,
                settings.embedding_dimensions,
            )
    except Exception as exc:
        logger.warning("LM Studio embedding fallback failed for %s: %s", purpose, exc)
    finally:
        if lmstudio is not None:
            try:
                await lmstudio.close()
            except Exception:
                pass

    cloud_embedding = await _cloud_semantic_embedding(text, purpose=purpose)
    if cloud_embedding is not None:
        return cloud_embedding

    return [0.0] * settings.embedding_dimensions, {
        "embedding_provider": "zero_vector",
        "embedding_fallback": "zero_vector",
        "embedding_fallback_reason": fallback_reason,
    }


async def embed_query(
    text: str,
    *,
    primary: Any | None = None,
    purpose: str = "query",
) -> tuple[list[float], dict[str, str]]:
    return await embed_text(
        text,
        primary=primary,
        purpose=purpose,
        fallback_reason=f"{purpose}_embedding_unavailable",
    )
