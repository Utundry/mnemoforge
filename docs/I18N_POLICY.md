# Documentation Language Policy

MnemoForge uses English as the default language for public-facing documentation.

## Public Documentation

The following files should stay English-first:

- `README.md`
- `SETUP.md`
- `CLIENT_SETUP.md`
- `STATUS.md`
- `docs/PUBLIC_RELEASE_CHECKLIST.md`
- `docs/USAGE_CONDITIONS.md`
- provider setup guides under `docs/`
- demo documentation under `demo/`

## Localized Documentation

Localized documents are welcome when they are explicit and discoverable. Use one of these patterns:

- `docs/i18n/<locale>/<name>.md`
- `<name>.<locale>.md` for a direct sibling translation

For example:

- `docs/i18n/ru/CLIENT_SETUP.md`
- `CLIENT_SETUP.ru.md`

Do not mix languages in the same public guide unless the non-English text is a deliberate quoted example.

## Code And Examples

- Keep comments and examples in public docs English unless the example demonstrates localization behavior.
- Avoid non-ASCII slogans, personal notes, or private shorthand in public release files.
- Keep provider names, command names, environment variables, and API fields unchanged.

## Internal Agent And Server Strings

- Keep agent-facing, tool-facing, server-facing, and diagnostic strings in code English-only.
- Use localized natural language only for direct user dialogue or explicitly localized UI/content surfaces.
- Treat mojibake or corrupted encoding in code as a defect: do not copy it into new modules, and replace it with clear English UTF-8/ASCII text or remove the obsolete branch when safe.
- MCP/tool contracts should remain language-stable so weaker agents can follow the workflow without language drift.

## Internal Notes

Historical or planning documents may remain in their original language temporarily, but public release readiness should track and gradually translate them before they are linked from public entry points.
