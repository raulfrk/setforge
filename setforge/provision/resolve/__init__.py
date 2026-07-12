"""Resolver subsystem (spec §B2) — upstream version+integrity resolution.

Separate from the :class:`~setforge.provision.protocol.Provisioner` ABC (D6):
a :class:`~setforge.provision.resolve.protocol.Resolver` queries upstream
read-only to produce a :class:`~setforge.provision.resolve.protocol.ResolvedPin`
(concrete version + ecosystem-natural integrity), performing NO host mutation.
This lets ``setforge lock`` run without the install path.

This foundation module ships the protocol + registry only; the per-ecosystem
resolvers (cargo/python/go/github_release/extension/plugin) and the ``lock``
verb land in later tasks.
"""
