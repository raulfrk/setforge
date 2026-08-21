"""Typed, deterministic dependency graphs for bundle capabilities."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from setforge.config import (
    BundleComponent,
    BundleSpec,
    Config,
    ExtensionPackage,
    Package,
    PluginPackage,
)
from setforge.errors import ConfigError


class CapabilityTargetKind(StrEnum):
    """The execution boundary owned by one bundle component."""

    PACKAGE = "package"
    PLUGIN = "plugin"
    EXTENSION = "extension"
    FILE = "file"


class CapabilityStatus(StrEnum):
    """Durable meaning of one target-group activation attempt."""

    ACTIVE = "active"
    FAILED = "failed"
    BLOCKED = "blocked"
    COMPENSATED = "compensated"
    RECOVERY_REQUIRED = "recovery-required"


@dataclass(frozen=True, slots=True)
class CapabilityNode:
    """One resolved graph node with its stable declared dependencies."""

    id: str
    target_kind: CapabilityTargetKind
    depends_on: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ConfigError("capability node id must be a non-empty string")
        if not isinstance(self.target_kind, CapabilityTargetKind):
            raise ConfigError("capability node target_kind must be a typed target")
        if not isinstance(self.depends_on, tuple) or any(
            not isinstance(dependency, str) or not dependency
            for dependency in self.depends_on
        ):
            raise ConfigError(
                "capability node depends_on must be a tuple of non-empty strings"
            )


@dataclass(frozen=True, slots=True)
class CapabilityGroup:
    """One target adapter activation in a frozen capability plan.

    The existing extension and plugin adapters reconcile one inventory at a
    time, so their graph boundary is a target group rather than an individual
    subprocess invocation.  ``node_ids`` preserves declaration order for
    diagnostics while ``depends_on`` names prerequisite target groups.
    """

    target_kind: CapabilityTargetKind
    node_ids: tuple[str, ...]
    depends_on: tuple[CapabilityTargetKind, ...]


@dataclass(frozen=True, slots=True)
class CapabilityActivation:
    """Result returned by a target adapter after one activation attempt."""

    status: CapabilityStatus
    changed: bool
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.status, CapabilityStatus) or self.status not in {
            CapabilityStatus.ACTIVE,
            CapabilityStatus.FAILED,
        }:
            raise ValueError("activation status must be active or failed")
        if not isinstance(self.changed, bool):
            raise ValueError("activation changed must be a bool")


@dataclass(frozen=True, slots=True)
class CapabilityTargetAction:
    """Preflight, activation, and optional compensation for one target kind."""

    target_kind: CapabilityTargetKind
    preflight: Callable[[], None]
    activate: Callable[[], CapabilityActivation]
    compensate: Callable[[], None] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.target_kind, CapabilityTargetKind):
            raise ValueError("target action kind must be a typed target")
        if not callable(self.preflight) or not callable(self.activate):
            raise ValueError("target action preflight and activate must be callable")
        if self.compensate is not None and not callable(self.compensate):
            raise ValueError("target action compensate must be callable when set")


@dataclass(frozen=True, slots=True)
class CapabilityOutcome:
    """Observable final state for one target group."""

    target_kind: CapabilityTargetKind
    node_ids: tuple[str, ...]
    status: CapabilityStatus
    detail: str = ""
    blocked_by: tuple[CapabilityTargetKind, ...] = ()


@dataclass(frozen=True, slots=True)
class CapabilityGraph:
    """A validated capability graph in deterministic application order."""

    nodes: tuple[CapabilityNode, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.nodes, tuple) or any(
            not isinstance(node, CapabilityNode) for node in self.nodes
        ):
            raise ConfigError(
                "capability graph nodes must be a tuple of CapabilityNode values"
            )
        _validate_nodes(self.nodes)
        self.groups()

    def ordered(self) -> tuple[CapabilityNode, ...]:
        """Return a declaration-stable topological ordering."""
        by_id = {node.id: node for node in self.nodes}
        order_index = {node.id: index for index, node in enumerate(self.nodes)}
        indegree = {node.id: len(node.depends_on) for node in self.nodes}
        dependents: dict[str, list[str]] = {node.id: [] for node in self.nodes}
        for node in self.nodes:
            for dependency in node.depends_on:
                dependents[dependency].append(node.id)

        ready = sorted(
            (node_id for node_id, degree in indegree.items() if degree == 0),
            key=order_index.__getitem__,
        )
        ordered: list[CapabilityNode] = []
        while ready:
            node_id = ready.pop(0)
            ordered.append(by_id[node_id])
            released: list[str] = []
            for dependent in dependents[node_id]:
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    released.append(dependent)
            ready = sorted(ready + released, key=order_index.__getitem__)
        if len(ordered) != len(self.nodes):  # pragma: no cover - validated first
            raise AssertionError("validated capability graph contains a cycle")
        return tuple(ordered)

    def execute(
        self, actions: Sequence[CapabilityTargetAction]
    ) -> tuple[CapabilityOutcome, ...]:
        """Preflight all targets, activate in order, and compensate on failure.

        This executor performs no persistence itself.  The install shell owns
        write-ahead checkpoints around each action; a missing compensation
        therefore becomes ``recovery-required`` instead of an invented undo.
        """
        groups = self.groups()
        action_by_kind = _validated_actions(groups, actions)
        for group in groups:
            action_by_kind[group.target_kind].preflight()

        outcomes: list[CapabilityOutcome] = []
        changed: list[CapabilityTargetKind] = []
        outcome_by_kind: dict[CapabilityTargetKind, CapabilityOutcome] = {}
        for group in groups:
            blocked_by = tuple(
                dependency
                for dependency in group.depends_on
                if outcome_by_kind[dependency].status is not CapabilityStatus.ACTIVE
            )
            if blocked_by:
                outcome = CapabilityOutcome(
                    target_kind=group.target_kind,
                    node_ids=group.node_ids,
                    status=CapabilityStatus.BLOCKED,
                    detail="prerequisite target failed",
                    blocked_by=blocked_by,
                )
                outcomes.append(outcome)
                outcome_by_kind[group.target_kind] = outcome
                continue
            activation = action_by_kind[group.target_kind].activate()
            outcome = CapabilityOutcome(
                target_kind=group.target_kind,
                node_ids=group.node_ids,
                status=activation.status,
                detail=activation.detail,
            )
            outcomes.append(outcome)
            outcome_by_kind[group.target_kind] = outcome
            if activation.changed:
                changed.append(group.target_kind)

        if all(outcome.status is CapabilityStatus.ACTIVE for outcome in outcomes):
            return tuple(outcomes)
        return _compensate(outcomes, changed, action_by_kind)

    def groups(self) -> tuple[CapabilityGroup, ...]:
        """Return target-level activations in stable dependency order.

        A node DAG can still induce a cycle once nodes sharing one global
        adapter are collapsed into target groups.  Reject that shape before
        any adapter is probed: serial target activation could not honor it.
        """
        by_id = {node.id: node for node in self.nodes}
        first_index: dict[CapabilityTargetKind, int] = {}
        members: dict[CapabilityTargetKind, list[str]] = {}
        dependencies: dict[CapabilityTargetKind, set[CapabilityTargetKind]] = {}
        for index, node in enumerate(self.nodes):
            first_index.setdefault(node.target_kind, index)
            members.setdefault(node.target_kind, []).append(node.id)
            dependencies.setdefault(node.target_kind, set())
            for dependency in node.depends_on:
                dependency_kind = by_id[dependency].target_kind
                if dependency_kind is not node.target_kind:
                    dependencies[node.target_kind].add(dependency_kind)

        indegree = {kind: len(edges) for kind, edges in dependencies.items()}
        dependents: dict[CapabilityTargetKind, list[CapabilityTargetKind]] = {
            kind: [] for kind in dependencies
        }
        for kind, edges in dependencies.items():
            for dependency in edges:
                dependents[dependency].append(kind)
        ready = sorted(
            (kind for kind, degree in indegree.items() if degree == 0),
            key=first_index.__getitem__,
        )
        ordered: list[CapabilityGroup] = []
        while ready:
            kind = ready.pop(0)
            ordered.append(
                CapabilityGroup(
                    target_kind=kind,
                    node_ids=tuple(members[kind]),
                    depends_on=tuple(
                        sorted(dependencies[kind], key=first_index.__getitem__)
                    ),
                )
            )
            released: list[CapabilityTargetKind] = []
            for dependent in dependents[kind]:
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    released.append(dependent)
            ready = sorted(ready + released, key=first_index.__getitem__)
        if len(ordered) != len(dependencies):
            raise ConfigError("bundle capability target groups have a dependency cycle")
        return tuple(ordered)


def build_capability_graph(bundle: BundleSpec, cfg: Config) -> CapabilityGraph:
    """Resolve and validate one bundle into its typed application graph."""
    nodes = tuple(_resolve_node(component, cfg) for component in bundle.components)
    return CapabilityGraph(nodes)


def combine_capability_graphs(
    named_graphs: Sequence[tuple[str, CapabilityGraph]],
) -> CapabilityGraph:
    """Combine bundle-local graphs using stable ``bundle.component`` IDs."""
    combined: list[CapabilityNode] = []
    for bundle_id, graph in named_graphs:
        for node in graph.nodes:
            combined.append(
                CapabilityNode(
                    id=_qualified_id(bundle_id, node.id),
                    target_kind=node.target_kind,
                    depends_on=tuple(
                        _qualified_id(bundle_id, dependency)
                        for dependency in node.depends_on
                    ),
                )
            )
    nodes = tuple(combined)
    return CapabilityGraph(nodes)


def _qualified_id(bundle_id: str, node_id: str) -> str:
    """Return an injective, readable encoding of a bundle-local node ID."""
    return f"{len(bundle_id)}:{bundle_id}{len(node_id)}:{node_id}"


def _validated_actions(
    groups: tuple[CapabilityGroup, ...], actions: Sequence[CapabilityTargetAction]
) -> dict[CapabilityTargetKind, CapabilityTargetAction]:
    action_by_kind: dict[CapabilityTargetKind, CapabilityTargetAction] = {}
    for action in actions:
        if action.target_kind in action_by_kind:
            raise ConfigError(
                f"duplicate capability target action: {action.target_kind.value}"
            )
        action_by_kind[action.target_kind] = action
    expected = {group.target_kind for group in groups}
    actual = set(action_by_kind)
    if actual != expected:
        missing = sorted(kind.value for kind in expected - actual)
        extra = sorted(kind.value for kind in actual - expected)
        raise ConfigError(
            "capability target actions do not match graph: "
            f"missing={missing}, extra={extra}"
        )
    return action_by_kind


def _compensate(
    outcomes: list[CapabilityOutcome],
    changed: list[CapabilityTargetKind],
    actions: dict[CapabilityTargetKind, CapabilityTargetAction],
) -> tuple[CapabilityOutcome, ...]:
    by_kind = {outcome.target_kind: outcome for outcome in outcomes}
    for kind in reversed(changed):
        current = by_kind[kind]
        compensate = actions[kind].compensate
        if compensate is None:
            by_kind[kind] = CapabilityOutcome(
                target_kind=kind,
                node_ids=current.node_ids,
                status=CapabilityStatus.RECOVERY_REQUIRED,
                detail="target changed but has no safe compensation",
            )
            continue
        try:
            compensate()
        except Exception as exc:  # adapter boundary; preserve recovery state
            by_kind[kind] = CapabilityOutcome(
                target_kind=kind,
                node_ids=current.node_ids,
                status=CapabilityStatus.RECOVERY_REQUIRED,
                detail=f"compensation failed: {exc}",
            )
        else:
            by_kind[kind] = CapabilityOutcome(
                target_kind=kind,
                node_ids=current.node_ids,
                status=CapabilityStatus.COMPENSATED,
                detail=current.detail,
            )
    return tuple(by_kind[outcome.target_kind] for outcome in outcomes)


def _resolve_node(component: BundleComponent, cfg: Config) -> CapabilityNode:
    if component.file is not None:
        target_kind = CapabilityTargetKind.FILE
    elif component.plugin is not None:
        raise ConfigError(
            f"bundle component {component.id!r} uses a 'plugin' source, "
            "which requires a declared plugin package reference"
        )
    else:
        if component.package is not None:
            package = cfg.packages.get(component.package)
            if package is None:
                raise ConfigError(
                    f"bundle component {component.id!r} references unknown "
                    f"package {component.package!r}"
                )
        else:
            package = _inline_package(component)
        if isinstance(package, PluginPackage):
            target_kind = CapabilityTargetKind.PLUGIN
        elif isinstance(package, ExtensionPackage):
            target_kind = CapabilityTargetKind.EXTENSION
        else:
            target_kind = CapabilityTargetKind.PACKAGE
    return CapabilityNode(
        id=component.id,
        target_kind=target_kind,
        depends_on=tuple(component.depends_on),
    )


def _inline_package(component: BundleComponent) -> Package:
    for field in BundleComponent._INLINE_FIELDS:
        value = getattr(component, field)
        if value is not None:
            if isinstance(value, str):
                break
            return value
    raise ConfigError(f"bundle component {component.id!r} has no package source")


def _validate_nodes(nodes: tuple[CapabilityNode, ...]) -> None:
    by_id: dict[str, CapabilityNode] = {}
    for node in nodes:
        if node.id in by_id:
            raise ConfigError(f"bundle has a duplicate component id: {node.id!r}")
        by_id[node.id] = node
    for node in nodes:
        for dependency in node.depends_on:
            if dependency not in by_id:
                raise ConfigError(
                    f"bundle component {node.id!r} depends_on unknown id {dependency!r}"
                )

    _reject_cycle(nodes, by_id)


def _reject_cycle(
    nodes: tuple[CapabilityNode, ...], by_id: dict[str, CapabilityNode]
) -> None:
    """Refuse every dependency cycle, including a self-edge."""
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visiting:
            raise ConfigError(f"bundle has a dependency cycle involving {node_id!r}")
        if node_id in visited:
            return
        visiting.add(node_id)
        for dependency in by_id[node_id].depends_on:
            visit(dependency)
        visiting.remove(node_id)
        visited.add(node_id)

    for node in nodes:
        visit(node.id)
