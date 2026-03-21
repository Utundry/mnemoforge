# Cloud LLM providers

`supermemory` now supports a configurable external LLM via OpenAI-compatible APIs.

## Recommended config

Use the generic `CLOUD_LLM_*` variables for new setups:

```env
CLOUD_LLM_PROVIDER=gemini
CLOUD_LLM_API_KEY=your_key_here
CLOUD_LLM_MODEL=gemini-2.5-flash
CLOUD_LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
```

Gemini official OpenAI-compatibility docs:

- https://ai.google.dev/gemini-api/docs/openai

## Legacy GLM config

Existing installations keep working with legacy `GLM_*` variables:

```env
GLM_API_KEY=your_key_here
GLM_MODEL=glm-4.5-air
GLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4
```

## Selection rules

- If `CLOUD_LLM_API_KEY` is set, `supermemory` uses `CLOUD_LLM_PROVIDER`, `CLOUD_LLM_MODEL`, and `CLOUD_LLM_BASE_URL`.
- If `CLOUD_LLM_API_KEY` is empty, `supermemory` falls back to legacy `GLM_*`.
- If `CLOUD_LLM_PROVIDER` is omitted, the provider label is inferred from model/base URL.
