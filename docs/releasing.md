# Releasing

How CI gates `main` and how a versioned release is cut.

## CI

Every push/PR to `main` runs [`.github/workflows/ci.yml`](../.github/workflows/ci.yml):

- **Unit tests** — `uv run pytest`.
- **Config validation** against the e2e test fixture —
  `uv run setforge validate --config=tests/fixtures/e2e/setforge.test.yaml --all`.
- **Secrets scan** — gitleaks.
- **E2E Docker tests** — the `tests/docker/` suite against a fresh container.

The engine repo no longer carries a root `setforge.yaml` (it lives in your
config repo), so CI validates against the e2e fixture instead.

## Cutting a release

Run the preflight script **before** pushing a `v*.*.*` tag:

```bash
uv run python scripts/release_preflight.py
```

It runs eight checks: `uv build` → `twine check` → temp `UV_TOOL_DIR` install →
`setforge --version` / `--help` / `__version__` assertions → workflow YAML
parse → an open-high-priority-issues check. It exits 0 on success, or non-zero
with the failing step name.

Once preflight is green, push `main` then the tag:

```bash
cd ~/setforge
git push origin main
git tag -a vX.Y.Z -m 'vX.Y.Z: summary'
git push origin vX.Y.Z
```

The tag push fires two workflows:

- [`publish-pypi.yml`](../.github/workflows/publish-pypi.yml) — `uv build` +
  `twine check` + PyPI upload. It is idempotent (`skip-existing: true`), so
  re-pushing the same tag is safe.
- [`release.yml`](../.github/workflows/release.yml) — `gh release create` with
  auto-generated notes for the commit range since the previous tag.

Verify on <https://pypi.org/project/setforge/> and the GitHub Releases tab.

### PyPI credentials

The publish workflow authenticates to PyPI via a `PYPI_API_TOKEN` secret in a
`pypi` GitHub environment. If neither that secret nor a configured PyPI Trusted
Publisher (with `id-token: write` on the job) is present, the upload step fails
while build + twine-check still pass — re-run the publish job after wiring up
credentials.
