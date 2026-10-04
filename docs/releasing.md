# Releasing

How CI gates `main` and how a versioned release is cut.

## CI

CI is split between GitHub and the home CI coordinator.

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs on every push and
pull request to `main`:

- **Secrets scan** — gitleaks, run by GitHub on `ubuntu-latest`.
- **Workbox trigger / unit** and **Workbox trigger / integration** — these
  allocate no runner. They only record a job for the home CI coordinator to
  poll; the coordinator then runs the checks on the exact SetForge commit
  (the integration trigger covers the Docker and mutation checks for pull
  requests).

[`.github/workflows/nightly.yml`](../.github/workflows/nightly.yml) runs on a
daily schedule (and manually) and records one **Workbox trigger / full** job.
The home CI coordinator runs the four nightly gates for it: Docker end-to-end,
canary, deep, and mutation.

The engine repo no longer carries a root `setforge.yaml` (it lives in your
config repo).

## Cutting a release

Run the preflight script **before** pushing a `v*.*.*` tag:

```bash
uv run python scripts/release_preflight.py
```

It runs seven checks: `uv build` → `twine check` → temp `UV_TOOL_DIR` install →
`setforge --version` / `--help` / `__version__` assertions → workflow YAML
parse. It exits 0 on success, or non-zero with the failing step name.

Once preflight is green, push `main` then the tag:

```bash
cd ~/setforge
git push origin main
git tag -a vX.Y.Z -m 'vX.Y.Z: summary'
git push origin vX.Y.Z
```

The tag push fires two workflows:

- [`publish-pypi.yml`](../.github/workflows/publish-pypi.yml) — currently
  disabled with `if: false` until PyPI publishing credentials are configured.
  Tag pushes therefore skip package build and upload.
- [`release.yml`](../.github/workflows/release.yml) — `gh release create` with
  auto-generated notes for the commit range since the previous tag.

Verify the GitHub Releases tab and confirm the PyPI workflow was skipped.

### PyPI credentials

Before enabling the publish job, configure either a `PYPI_API_TOKEN` secret in
the `pypi` GitHub environment or a PyPI Trusted Publisher with `id-token: write`.
Enabling publication is a separate release-infrastructure change; do not remove
the guard merely to cut a GitHub release.
