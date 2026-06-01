# GLM/Zhipu AI API Integration

This guide explains how to configure GLM/Zhipu AI as an optional cloud LLM provider for SloplessCode.

GLM is no longer treated as a single hard-coded fallback path. Prefer the provider-profile configuration described in [CLOUD_LLM_PROVIDERS.md](CLOUD_LLM_PROVIDERS.md) when you need multi-provider failover.

## Get An API Key

1. Open the official console: <https://open.bigmodel.cn/>
2. Sign in or create an account.
3. Create an API key in the API Keys section.
4. Store the key outside git, for example in your local `.env` file or secret manager.

## Basic Configuration

Set GLM-specific variables only in a private `.env` file:

```env
GLM_API_KEY=<your-key>
GLM_MODEL=glm-4.5-air
GLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4
```

For the generic cloud gateway, use an OpenAI-compatible profile when available:

```env
CLOUD_LLM_MODEL_PROFILES={"glm-4.5-air":{"provider":"glm","api_style":"openai-chat","api_key_env":"GLM_API_KEY","base_url":"https://open.bigmodel.cn/api/paas/v4","model":"glm-4.5-air","enabled":true}}
BALANCED_CLOUD_LLMS=glm-4.5-air
```

Keep JSON values on a single line in dotenv files.

## Usage In Code

```python
from app.services.cloud_llm import cloud_available, cloud_complete


async def example_usage() -> str:
    if not cloud_available():
        return "No cloud LLM is configured."

    return await cloud_complete(
        prompt="Summarize the current task status.",
        system="You are a concise engineering assistant.",
        max_tokens=2048,
        temperature=0.3,
    )
```

## Function Parameters

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `prompt` | `str` | required | User or task prompt sent to the model. |
| `system` | `str` | `"You are a helpful assistant."` | System prompt. |
| `max_tokens` | `int` | `2048` | Maximum generated tokens. |
| `temperature` | `float` | `0.3` | Sampling temperature. |
| `timeout` | `float` | `60.0` | Request timeout in seconds. |

## Model Notes

Common GLM/Zhipu model choices include:

| Model | Typical Use |
| --- | --- |
| `glm-4.5-air` | Balanced default for general tasks. |
| `glm-4.5-flash` | Lower-latency responses. |
| `glm-4-plus` | Heavier reasoning and analysis tasks. |

Check the provider documentation for the current model list, pricing, and limits before relying on a model in production.

## Smoke Test

If a local test script is available in your checkout, run:

```bash
python scripts/test_glm_api.py
```

The smoke test should verify:

- API reachability;
- one basic completion request;
- timeout and error handling;
- fallback behavior when the provider is unavailable.

## Troubleshooting

### "No cloud LLM configured"

Check that the API key variable referenced by the active profile is set in your runtime environment.

### Timeout Or Slow Responses

- Lower `max_tokens`.
- Increase the timeout for long tasks.
- Check provider status, account limits, and network connectivity.

### Low-Quality Responses

- Lower `temperature` for deterministic technical work.
- Improve the system prompt and include constraints.
- Try a stronger model for consolidation or conflict-resolution tasks.

### API Key Does Not Work

- Confirm the key was copied completely.
- Create a new key if the old one was revoked.
- Check account balance, quotas, and regional availability.

## Integration Points

When configured, cloud LLM providers may be used by:

- `app/services/ai_dir_parser.py`
- `app/services/skill_crystallizer.py`
- `app/services/task_router.py`
- `app/services/normalization_service.py`

Provider failures should be handled as degraded provider state, not as a full server failure when another configured LLM path is available.

## Resources

- Official GLM/Zhipu API documentation: <https://open.bigmodel.cn/dev/api>
- Model list: <https://open.bigmodel.cn/dev/api#models>
- Pricing and limits: <https://open.bigmodel.cn/pricing>
