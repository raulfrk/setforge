"""``install`` when a marketplace cache directory holds a different repo.

Under ``claude.install_mode: local-clone`` the install stops while planning, with
an error that tells the user what to do, before it deploys or changes anything.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from .conftest import IntegrationEnv

pytestmark = pytest.mark.integration

_HOST_LOCAL = """\
claude:
  install_mode: local-clone
marketplaces:
  add:
    bobs:
      source: github
      repo: bob/tools
plugins:
  add: [helper@bobs]
"""


def test_install_refuses_a_colliding_cache_dir_and_deploys_nothing(
    integration_env: Callable[..., IntegrationEnv],
    integration_subprocess,
    tmp_path: Path,
) -> None:
    env = integration_env()
    env.local_config.parent.mkdir(parents=True, exist_ok=True)
    env.local_config.write_text(_HOST_LOCAL, encoding="utf-8")
    claude = str(env.present_binary("claude"))
    integration_subprocess.register([claude, "plugin", "list", "--json"], stdout="[]")
    integration_subprocess.register(
        [claude, "plugin", "marketplace", "list", "--json"], stdout="[]"
    )
    cache_dir = tmp_path / "mp" / "tools"
    cache_dir.mkdir(parents=True)
    (cache_dir / "marker.txt").write_text("alice's clone", encoding="utf-8")
    for git in {"git", shutil.which("git") or "git"}:
        integration_subprocess.register(
            [git, "-C", str(cache_dir), "remote", "get-url", "origin"],
            stdout="https://github.com/alice/tools.git\n",
        )

    result = env.run_verb(["install", "--yes", "--no-git-check", "--no-secrets-scan"])

    assert result.exit_code != 0, result.output
    message = str(result.exception)
    assert "marketplace 'bobs'" in message
    assert str(cache_dir) in message
    assert "'https://github.com/alice/tools.git'" in message
    assert "'bob/tools'" in message
    assert "Nothing was changed" in message
    assert f"rm -rf {cache_dir}" in message
    assert (cache_dir / "marker.txt").read_text(encoding="utf-8") == "alice's clone"
    assert not env.live(".setforge_it").exists()
