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

## Internal Notes

Historical or planning documents may remain in their original language temporarily, but public release readiness should track and gradually translate them before they are linked from public entry points.
