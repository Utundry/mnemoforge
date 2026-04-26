# Cloud LLM providers

`supermemory` supports configurable external LLM profiles with explicit API style selection.

## Recommended config

Use the generic `CLOUD_LLM_*` variables for a single default provider:

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

## Multi-provider failover

To let `llm_gateway` continue work on an alternate cloud model when the primary model hits quota, rate limits, or context pressure, add extra profiles through `CLOUD_LLM_MODEL_PROFILES`.

Example: native Gemini plus OpenAI-compatible GLM:

```env
GEMINI_API_KEY=your_gemini_key_here
GLM_API_KEY=your_glm_key_here
CLOUD_LLM_MODEL_PROFILES={"gemini-3-flash-preview":{"provider":"gemini","api_style":"gemini-native","api_key_env":"GEMINI_API_KEY","base_url":"https://generativelanguage.googleapis.com/v1beta","model":"gemini-3-flash-preview"},"glm-4.5-air":{"provider":"glm","api_style":"openai-chat","api_key_env":"GLM_API_KEY","base_url":"https://api.z.ai/api/paas/v4","model":"glm-4.5-air"}}

ECONOMY_CLOUD_LLMS=gemini-3-flash-preview,glm-4.5-air
BALANCED_CLOUD_LLMS=glm-4.5-air,gemini-3-flash-preview
REASONING_CLOUD_LLMS=glm-4.5-air,gemini-3-flash-preview
```

Notes:

- `CLOUD_LLM_MODEL_PROFILES` accepts either a JSON object or a list of profile objects.
- Each profile needs `provider`, `base_url`, and either `api_key` or `api_key_env`.
- Profiles may include `enabled` or `active`; both default to `true`. Set one to `false` to keep a model documented but out of routing.
- `api_style` controls how requests are built.
- Supported `api_style` values today are `openai-chat` and `gemini-native`.
- Legacy `GLM_*` config is automatically treated as an available cloud model profile, so it participates in failover without extra JSON.
- A disabled profile with the same model id as a legacy/first-class config disables that configured model too.
- If `api_style` is omitted, the client defaults to `openai-chat`, except Gemini URLs without `/openai`, which are treated as `gemini-native`.

## DeepSeek-first routing

DeepSeek uses the OpenAI-compatible chat format:

```env
DEEPSEEK_API_KEY=your_deepseek_key_here
CLOUD_LLM_MODEL_PROFILES={"deepseek-chat":{"provider":"deepseek","api_style":"openai-chat","api_key_env":"DEEPSEEK_API_KEY","base_url":"https://api.deepseek.com","model":"deepseek-chat","enabled":true},"glm-4.7":{"provider":"glm","model":"glm-4.7","enabled":false},"gemini-3-flash-preview":{"provider":"gemini","model":"gemini-3-flash-preview","enabled":false}}

ECONOMY_CLOUD_LLMS=deepseek-chat
BALANCED_CLOUD_LLMS=deepseek-chat
REASONING_CLOUD_LLMS=deepseek-chat
```

## Gemini native format

For official Google Gemini request format, use:

```env
CLOUD_LLM_MODEL_PROFILES={"gemini-3-flash-preview":{"provider":"gemini","api_style":"gemini-native","api_key_env":"GEMINI_API_KEY","base_url":"https://generativelanguage.googleapis.com/v1beta","model":"gemini-3-flash-preview"}}
```

This maps to:

- `POST /models/{model}:generateContent`
- header `x-goog-api-key`
- body `contents[].parts[].text`

## Runtime verification

After restarting the server, check:

- `GET /api/v1/health`
- `GET /api/v1/system/info`

Both responses now expose:

- `llm.cloud_available`
- `llm.default_cloud_provider`
- `llm.configured_cloud_models`
- `llm.gateway.*` routing lists

If only `glm-4.5-air` appears in `configured_cloud_models`, then only GLM is currently available for cloud failover and you still need to add the second provider key/profile.
