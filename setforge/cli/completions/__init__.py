"""Vendored shell completion templates for ``setforge completion install``.

Files in this package are loaded via :func:`importlib.resources.files`
as a fallback when ``setforge --show-completion=<shell>`` subprocess
invocation fails (non-zero exit / empty stdout / FileNotFoundError /
timeout). Seeded from typer-generated output at commit time; a CI drift
gate keeps them in sync.
"""
