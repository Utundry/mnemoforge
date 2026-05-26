FROM python:3.13-slim

WORKDIR /app

ARG MNEMOFORGE_GIT_COMMIT=unknown
ARG MNEMOFORGE_BUILD_TAG=unknown
ARG MNEMOFORGE_IMAGE_REPOSITORY=unknown

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY static/ static/
COPY mcp/ mcp/
COPY cli/ cli/
COPY scripts/ scripts/
COPY docs/ docs/
COPY demo/ demo/
COPY README.md SETUP.md CLIENT_SETUP.md STATUS.md .env.public.example ./

RUN mkdir -p qdrant_data

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MNEMOFORGE_GIT_COMMIT=$MNEMOFORGE_GIT_COMMIT \
    MNEMOFORGE_BUILD_TAG=$MNEMOFORGE_BUILD_TAG \
    MNEMOFORGE_IMAGE_REPOSITORY=$MNEMOFORGE_IMAGE_REPOSITORY

LABEL org.opencontainers.image.title="SloplessCode" \
      org.opencontainers.image.revision=$MNEMOFORGE_GIT_COMMIT \
      org.opencontainers.image.version=$MNEMOFORGE_BUILD_TAG \
      org.opencontainers.image.source="https://github.com/Utundry/sloplesscode" \
      org.opencontainers.image.description="SloplessCode (formerly Mnemoforge)"

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
