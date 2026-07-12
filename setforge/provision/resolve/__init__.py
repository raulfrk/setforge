"""Resolver subsystem — upstream version+integrity resolution.

Separate from the :class:`~setforge.provision.protocol.Provisioner` ABC:
a :class:`~setforge.provision.resolve.protocol.Resolver` queries upstream
read-only to produce a :class:`~setforge.provision.resolve.protocol.ResolvedPin`
(concrete version + ecosystem-natural integrity), performing NO host mutation.
This lets ``setforge lock`` run without the install path.
"""
