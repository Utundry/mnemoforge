# SloplessCode Status

Current release target: `GitHub alpha`

## Positioning

SloplessCode is a local-first knowledge and coordination substrate for coding agents.

Release name note: `SloplessCode` is the public-facing name. `mnemoforge` remains
the legacy runtime/project id for compatibility, and `sloplesscode` is accepted as
the public alias.

Current focus:
- governed project knowledge
- MCP agent access
- storage trust, integrity, and data hygiene
- external-project bootstrap
- multi-agent coordination

## Working Well

- core memories and retrieval
- project context assembly
- laws, improvements, tasks, docs projection
- MCP SSE and onboarding basics
- data hygiene and integrity admin workflows
- functionality release-scope review

## Still Alpha

- public packaging and documentation cleanup
- storage integrity forensic maturity
- dataset-boundary cleanup on live stores
- external-project bootstrap polish
- optional legacy/experimental modules remain behind recommended disable flags

## Recommended Alpha Defaults

- disable experimental modules by default:
  - `auto_memory`
  - `code_search`
  - `layout_fixer`
  - `log_filter`
  - `openai_compat`
- do not ship live service data
- prefer a safe demo dataset for public examples
- run the public release checklist before publishing

## Near-Term Priorities

1. Finish public-doc cleanup for setup and client-facing guides.
2. Continue data hygiene and integrity remediation maturity.
3. Prepare a safe public demo path and GitHub quickstart.
4. Publish and maintain clear public usage conditions for release artifacts.
