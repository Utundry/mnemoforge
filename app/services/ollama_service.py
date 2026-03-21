import logging
from typing import Optional

import httpx

from app.config import settings
from app.core.exceptions import EmbeddingServiceError

logger = logging.getLogger(__name__)


class OllamaService:
    def __init__(
        self,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = 120.0,
    ):
        self.base_url = (base_url or settings.ollama_base_url).rstrip("/")
        self.model = model or settings.ollama_embedding_model
        self._client = httpx.AsyncClient(timeout=timeout)

    async def embed(self, text: str) -> list[float]:
        """Return embedding vector for a single text string."""
        try:
            response = await self._client.post(
                f"{self.base_url}/api/embed",
                json={"model": self.model, "input": text},
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            logger.error("Ollama HTTP error: %s", e)
            raise EmbeddingServiceError(f"Ollama returned {e.response.status_code}")
        except httpx.RequestError as e:
            logger.error("Ollama connection error: %s", e)
            raise EmbeddingServiceError("Cannot connect to Ollama")

        data = response.json()
        # Ollama /api/embed returns {"embeddings": [[...]]}
        embeddings = data.get("embeddings")
        if embeddings and isinstance(embeddings, list) and embeddings[0]:
            return embeddings[0]
        raise EmbeddingServiceError("Unexpected response format from Ollama")

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Return embeddings for multiple texts (sequential calls)."""
        results = []
        for text in texts:
            results.append(await self.embed(text))
        return results

    async def health(self) -> bool:
        try:
            r = await self._client.get(f"{self.base_url}/api/tags", timeout=5.0)
            return r.status_code == 200
        except Exception:
            return False

    async def generate(self, prompt: str, model: str | None = None, timeout: float = 120.0) -> str:
        """Generate text via Ollama /api/generate. Returns empty string on failure."""
        try:
            response = await self._client.post(
                f"{self.base_url}/api/generate",
                json={"model": model or self.model, "prompt": prompt, "stream": False,
                      "think": False},  # disable thinking mode for faster responses
                timeout=timeout,
            )
            response.raise_for_status()
            return response.json().get("response", "").strip()
        except Exception:
            return ""

    async def close(self) -> None:
        await self._client.aclose()
