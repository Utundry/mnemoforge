# Public Release Checklist

Status: active
Project: `mnemoforge`

Use this checklist before publishing a GitHub alpha or Docker Hub image.

## Before Packaging

1. Confirm the release uses `SELF_PROJECT_ID=mnemoforge`.
2. Use `.env.public.example` as the public template.
3. Keep `API_KEY` empty in the template and require operators to set it before network exposure.
4. Keep experimental modules disabled by default:
   `auto_memory,code_search,layout_fixer,log_filter,openai_compat`.
5. Confirm public examples use the synthetic demo dataset in `demo/`.
6. Do not include live service data, private notes, database files, backups, logs, or local temp files.

## Readiness Checks

Run the public bootstrap check:

```bash
python scripts/bootstrap_public_release.py --check
```

Run the release artifact audit:

```bash
python scripts/audit_release_artifacts.py
```

Run the targeted Docker test contour before packaging:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_pytest_docker.ps1 tests/test_public_release_config.py tests/test_publish_docker_image.py tests/test_publish_readiness.py tests/test_usage_conditions.py -q
```

## Docker Hub Publish

1. Build and inspect the image locally before pushing.
2. Keep the repository and tag explicit and separate.
3. Push only after the readiness checks pass.

```bash
python scripts/publish_docker_image.py --repository yourname/mnemoforge --tag latest --push
```

## Public FAQ

**Can I publish my local database with the image?**

No. Public releases must use synthetic or redacted data only.

**Should public users copy `.env.example`?**

No. Public users should start from `.env.public.example`; `.env.example` may contain internal development options.

**Is MnemoForge stable enough for production?**

Not yet. The current target is a GitHub alpha with local-first defaults and documented usage conditions.

**What must be enabled before exposing the server outside localhost?**

Set `API_KEY` at minimum, then review filesystem roots, request size limits, and LLM rate limits.
