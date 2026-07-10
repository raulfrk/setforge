"""Uniform package-provisioning protocol (Epic B foundation).

Every ecosystem provisioner (cargo, python, go, github_release, …) implements
the single :class:`~setforge.provision.protocol.Provisioner` ABC so the driver
owns the cross-cutting invariants (exit-gating, REPORT-no-write, idempotent
skip) once instead of once per ecosystem.
"""
