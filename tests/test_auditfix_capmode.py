"""Regression: capture preserves the tracked file's permission bits.

``_write_if_changed`` rewrites the tracked source-of-truth via
``atomicio.atomic_write_text``. Without an explicit ``mode=``, the 0600
``mkstemp`` default rides in through ``os.replace`` and silently demotes
the tracked file's permissions — an executable hook (0o755) or a 0o644
config becomes 0o600, and that corrupted mode propagates cross-host via
the shared config repo and onto live on the next deploy.

The writeback must carry the EXISTING tracked mode across the rewrite,
and fall back to 0o644 (not the 0600 mkstemp leftover) for a fresh
tracked file.
"""

import stat
from pathlib import Path

from setforge.capture import CaptureAction, _write_if_changed


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def test_write_if_changed_preserves_executable_mode(tmp_path: Path) -> None:
    src = tmp_path / "tracked" / "hook.sh"
    src.parent.mkdir(parents=True)
    src.write_text("#!/bin/sh\necho old\n", encoding="utf-8")
    src.chmod(0o755)

    result = _write_if_changed(src, "#!/bin/sh\necho new\n")

    assert result.action is CaptureAction.UPDATED
    assert _mode(src) == 0o755
    assert src.read_text(encoding="utf-8") == "#!/bin/sh\necho new\n"


def test_write_if_changed_preserves_0644_mode(tmp_path: Path) -> None:
    src = tmp_path / "tracked" / "config.toml"
    src.parent.mkdir(parents=True)
    src.write_text("k = 1\n", encoding="utf-8")
    src.chmod(0o644)

    result = _write_if_changed(src, "k = 2\n")

    assert result.action is CaptureAction.UPDATED
    assert _mode(src) == 0o644


def test_write_if_changed_fresh_file_defaults_to_0644(tmp_path: Path) -> None:
    src = tmp_path / "tracked" / "new.txt"
    src.parent.mkdir(parents=True)
    # src does not exist yet.

    result = _write_if_changed(src, "fresh content\n")

    assert result.action is CaptureAction.UPDATED
    assert _mode(src) == 0o644
