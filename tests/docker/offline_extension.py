"""Hermetic VS Code extension fixture used by Docker E2E tests."""

from __future__ import annotations

import io
import json
import os
import sys
from collections.abc import Sequence
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

FIXTURE_EXTENSION_ID = "setforge.fixture-extension"
FIXTURE_VSIX_PATH = "/tmp/setforge-fixture-extension.vsix"
REAL_CODE_PATH = "/usr/bin/code"

_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_PACKAGE = {
    "name": "fixture-extension",
    "displayName": "SetForge Fixture Extension",
    "version": "1.0.0",
    "publisher": "setforge",
    "engines": {"vscode": "*"},
    "categories": ["Other"],
}
_MANIFEST = """\
<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">
  <Metadata>
    <Identity Language="en-US" Id="fixture-extension" Version="1.0.0"
              Publisher="setforge" />
    <DisplayName>SetForge Fixture Extension</DisplayName>
    <Description xml:space="preserve">Offline fixture for SetForge E2E.</Description>
  </Metadata>
  <Installation>
    <InstallationTarget Id="Microsoft.VisualStudio.Code" Version="[1.0.0,)" />
  </Installation>
  <Dependencies />
  <Assets>
    <Asset Type="Microsoft.VisualStudio.Code.Manifest"
           Path="extension/package.json" Addressable="true" />
  </Assets>
</PackageManifest>
"""
_CONTENT_TYPES = """\
<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="json" ContentType="application/json" />
  <Default Extension="vsixmanifest" ContentType="text/xml" />
</Types>
"""


def fixture_vsix_bytes() -> bytes:
    """Return a deterministic, minimal VSIX for ``FIXTURE_EXTENSION_ID``."""
    entries = {
        "[Content_Types].xml": _CONTENT_TYPES,
        "extension.vsixmanifest": _MANIFEST,
        "extension/package.json": json.dumps(
            _PACKAGE, sort_keys=True, separators=(",", ":")
        ),
    }
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            info = ZipInfo(name, date_time=_ZIP_TIMESTAMP)
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, content.encode())
    return buffer.getvalue()


def rewritten_code_argv(args: Sequence[str]) -> list[str]:
    """Map the fixture install ID to its local VSIX; delegate every other argv."""
    forwarded = list(args)
    if forwarded == ["--install-extension", FIXTURE_EXTENSION_ID]:
        forwarded[-1] = FIXTURE_VSIX_PATH
    return [REAL_CODE_PATH, *forwarded]


def main() -> None:
    """Replace this adapter process with the real VS Code CLI."""
    argv = rewritten_code_argv(sys.argv[1:])
    os.execv(REAL_CODE_PATH, argv)


if __name__ == "__main__":  # pragma: no cover - exercised through Docker
    main()
