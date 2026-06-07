# Schema-compat corpus (frozen)

One directory per `schema_version`, each holding a single
`setforge.yaml` that is a frozen, same-writer snapshot of a
minimal-but-valid config at that version:

- `1.0/` — the key-ABSENT baseline (no `schema_version:` key; every
  pre-versioning config is implicitly 1.0).
- `1.1/` — `schema_version: '1.1'`.
- `1.2/` — `schema_version: '1.2'`.

`tests/test_schema_compat_matrix.py` reads these to prove the production
migration chain (`setforge.migrations.MIGRATIONS`) is bidirectionally
safe — forward-migrating every fixture to the current version,
down-migrating the current fixture to the 1.0 baseline byte-identically,
and refusing major-newer / warning on minor-newer configs.

## Two rules

1. **ADD a directory per new `schema_version`.** When a migration bumps
   `current_expected_schema_version`, generate a new `<version>/` fixture
   so the matrix exercises the new endpoint. The growth guard
   (`test_corpus_covers_exactly_known_versions`) fails until the dir set
   equals `setforge.migrations.known_versions()`.

2. **NEVER edit a frozen fixture's bytes.** The fixtures are the
   historical record the migration chain is tested against; editing one
   silently weakens the regression. In particular, arm (iv) asserts
   byte-identity against these bytes (normalized through the in-run ruamel
   instance), so an edit can mask a real downgrade regression.

## Provenance — how to (re)generate

The fixtures are NOT hand-authored. They are serialized through the
PRODUCTION writer (`setforge.migrations._yaml_ops.atomic_write_yaml`) so
they are byte-for-byte what a real migrate write lands on disk. To
regenerate after registering a new version:

```sh
uv run python -c "import tests.test_schema_compat_matrix as m; m._regenerate_corpus()"
```

Then commit the new directory's bytes. Do not re-run this against an
existing frozen fixture expecting a diff — same writer, same bytes.
