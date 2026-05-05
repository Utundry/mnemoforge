from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class LMStudioService:
    """Small OpenAI-compatible client for LM Studio local generation."""

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.base_url = (base_url or settings.lmstudio_base_url).rstrip("/")
        self.model = model or settings.lmstudio_model
        self.embedding_model = os.getenv("LMSTUDIO_EMBEDDING_MODEL", "").strip()
        self._client = httpx.AsyncClient(timeout=timeout)
        self._resolved_model: str | None = None
        self._resolved_embedding_model: str | None = None

    async def health(self) -> bool:
        try:
            response = await self._client.get(f"{self.base_url}/models", timeout=5.0)
            return response.status_code == 200
        except Exception:
            return False

    async def list_models(self) -> list[str]:
        try:
            response = await self._client.get(f"{self.base_url}/models", timeout=5.0)
            response.raise_for_status()
            data = response.json()
            models = data.get("data", [])
            result: list[str] = []
            if isinstance(models, list):
                for item in models:
                    if not isinstance(item, dict):
                        continue
                    model_id = str(item.get("id") or "").strip()
                    if model_id:
                        result.append(model_id)
            return result
        except Exception as exc:
            logger.debug("LM Studio model listing failed: %s", exc)
            return []

    async def resolve_model(self, requested: str | None = None) -> str:
        model = (requested or self.model or "").strip()
        if model and model.lower() not in {"auto", "local-model"}:
            return model
        if self._resolved_model:
            return self._resolved_model
        models = await self.list_models()
        for candidate in models:
            lowered = candidate.lower()
            if "embed" in lowered or "embedding" in lowered:
                continue
            self._resolved_model = candidate
            return candidate
        self._resolved_model = models[0] if models else model or "local-model"
        return self._resolved_model

    async def resolve_embedding_model(self, requested: str | None = None) -> str:
        model = (requested or self.embedding_model or "").strip()
        if model and model.lower() not in {"auto", "local-model"}:
            return model
        if self._resolved_embedding_model:
            return self._resolved_embedding_model
        models = await self.list_models()
        for candidate in models:
            lowered = candidate.lower()
            if "embed" in lowered or "embedding" in lowered:
                self._resolved_embedding_model = candidate
                return candidate
        self._resolved_embedding_model = model or self.model or "local-model"
        return self._resolved_embedding_model

    async def embed(self, text: str, model: str | None = None, timeout: float = 120.0) -> list[float]:
        try:
            resolved_model = await self.resolve_embedding_model(model)
            response = await self._client.post(
                f"{self.base_url}/embeddings",
                json={"model": resolved_model, "input": text},
                timeout=timeout,
            )
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            embedding = data.get("data", [{}])[0].get("embedding")
            if isinstance(embedding, list) and embedding:
                return [float(item) for item in embedding]
        except Exception as exc:
            logger.debug("LM Studio embedding failed: %s", exc)
        return []

    async def generate(self, prompt: str, model: str | None = None, timeout: float = 120.0) -> str:
        try:
            resolved_model = await self.resolve_model(model)
            response = await self._client.post(
                f"{self.base_url}/chat/completions",
                json={
                    "model": resolved_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.2,
                    "stream": False,
                },
                timeout=timeout,
            )
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            message = data.get("choices", [{}])[0].get("message", {})
            content = message.get("content")
            return content.strip() if isinstance(content, str) else ""
        except Exception as exc:
            logger.debug("LM Studio generation failed: %s", exc)
            return ""

    async def close(self) -> None:
        await self._client.aclose()
