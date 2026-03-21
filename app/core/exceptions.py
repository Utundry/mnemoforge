from fastapi import HTTPException, status


class MemoryNotFoundError(HTTPException):
    def __init__(self, memory_id: str):
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Memory '{memory_id}' not found",
        )


class EmbeddingServiceError(HTTPException):
    def __init__(self, detail: str = "Embedding service unavailable"):
        super().__init__(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=detail,
        )


class QdrantServiceError(HTTPException):
    def __init__(self, detail: str = "Vector database unavailable"):
        super().__init__(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=detail,
        )


class VectorDimensionMismatchError(HTTPException):
    def __init__(self, detail: str = "Vector dimension mismatch"):
        super().__init__(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=detail,
        )
