# Usage Conditions

Status: active
Project: `mnemoforge`

This document describes the operational conditions for using and publishing SloplessCode.
It is a practical release policy, not legal advice.

## Core Conditions

1. Use the system with a valid `SELF_PROJECT_ID` and a known release profile.
2. Do not publish live service data, private project notes, secrets, or internal operator-only memories in public releases.
3. Treat `README.md`, setup guides, and public status pages as public-facing artifacts; keep internal planning notes out of them.
4. Keep experimental or unstable modules disabled by default in public builds.
5. Require explicit API key configuration when the server is reachable outside localhost.
6. Validate release readiness before public distribution.

## Data Conditions

1. Public examples must use synthetic or redacted data.
2. Docker build contexts must exclude local caches, temp directories, database files, backups, and environment files.
3. Human-facing summaries should not expose internal-only secrets, local paths, or private network identifiers.
4. If a document mixes public and private material, split it before release.

## Distribution Conditions

1. Publish public images only from a release template that passes the public-release checks.
2. Use a clearly named public environment template instead of the internal `.env.example`.
3. Keep Docker Hub tags and repositories explicit and separate.
4. Update public docs when defaults change so the published instructions stay reproducible.

## Support Conditions

1. The project may change without preserving compatibility with unpublished internal workflows.
2. Public users should rely on the documented public template, readiness checks, and quickstart instructions.
3. Internal operator workflows may remain private and are not part of the public contract.
