"""Focused contracts for typed bundle capability graphs."""

from __future__ import annotations

from pathlib import Path

import pytest

from setforge.config import (
    BundleComponent,
    BundleSpec,
    CargoPackage,
    Config,
    ExtensionPackage,
    FileComponent,
    PluginPackage,
    Profile,
    TrackedFile,
)
from setforge.errors import ConfigError
from setforge.provision.capability_graph import (
    CapabilityActivation,
    CapabilityGraph,
    CapabilityNode,
    CapabilityStatus,
    CapabilityTargetAction,
    CapabilityTargetKind,
    build_capability_graph,
    combine_capability_graphs,
)


def _cfg() -> Config:
    return Config(
        tracked_files={"dotfile": TrackedFile(src=Path("dotfile"), dst=".dotfile")},
        packages={
            "ripgrep": CargoPackage(crate="ripgrep"),
            "theme": ExtensionPackage(extension="example.theme"),
            "review": PluginPackage(plugin="review@team"),
        },
        profiles={"default": Profile()},
    )


def test_graph_discriminates_all_current_target_kinds_and_orders_stably() -> None:
    graph = build_capability_graph(
        BundleSpec(
            components=[
                BundleComponent(id="plugin", package="review", depends_on=["theme"]),
                BundleComponent(id="file", file=FileComponent(src=Path("x"), dst=".x")),
                BundleComponent(id="package", package="ripgrep"),
                BundleComponent(id="theme", package="theme", depends_on=["package"]),
            ]
        ),
        _cfg(),
    )

    assert [(node.id, node.target_kind) for node in graph.nodes] == [
        ("plugin", CapabilityTargetKind.PLUGIN),
        ("file", CapabilityTargetKind.FILE),
        ("package", CapabilityTargetKind.PACKAGE),
        ("theme", CapabilityTargetKind.EXTENSION),
    ]
    assert [node.id for node in graph.ordered()] == [
        "file",
        "package",
        "theme",
        "plugin",
    ]
    assert [
        (group.target_kind, group.node_ids, group.depends_on)
        for group in graph.groups()
    ] == [
        (CapabilityTargetKind.FILE, ("file",), ()),
        (CapabilityTargetKind.PACKAGE, ("package",), ()),
        (CapabilityTargetKind.EXTENSION, ("theme",), (CapabilityTargetKind.PACKAGE,)),
        (CapabilityTargetKind.PLUGIN, ("plugin",), (CapabilityTargetKind.EXTENSION,)),
    ]


@pytest.mark.parametrize(
    ("components", "match"),
    [
        (
            [
                BundleComponent(id="same", package="ripgrep"),
                BundleComponent(id="same", package="theme"),
            ],
            "duplicate",
        ),
        ([BundleComponent(id="one", package="ripgrep", depends_on=["gone"])], "gone"),
        (
            [
                BundleComponent(id="one", package="ripgrep", depends_on=["two"]),
                BundleComponent(id="two", package="theme", depends_on=["one"]),
            ],
            "cycle",
        ),
    ],
)
def test_graph_refuses_invalid_edges(
    components: list[BundleComponent], match: str
) -> None:
    with pytest.raises(ConfigError, match=match):
        build_capability_graph(BundleSpec(components=components), _cfg())


def test_graph_refuses_unroutable_direct_plugin_source() -> None:
    with pytest.raises(ConfigError, match="declared plugin package reference"):
        build_capability_graph(
            BundleSpec(components=[BundleComponent(id="plugin", plugin="review@team")]),
            _cfg(),
        )


def test_graph_refuses_a_cycle_created_by_global_target_grouping() -> None:
    with pytest.raises(ConfigError, match="target groups have a dependency cycle"):
        build_capability_graph(
            BundleSpec(
                components=[
                    BundleComponent(
                        id="pkg-after-ext", package="ripgrep", depends_on=["ext"]
                    ),
                    BundleComponent(id="ext", package="theme"),
                    BundleComponent(
                        id="ext-after-pkg", package="theme", depends_on=["pkg"]
                    ),
                    BundleComponent(id="pkg", package="ripgrep"),
                ]
            ),
            _cfg(),
        )


def test_executor_preflights_every_target_before_activation() -> None:
    graph = build_capability_graph(
        BundleSpec(
            components=[
                BundleComponent(
                    id="file",
                    file=FileComponent(src=Path("x"), dst=".x"),
                    depends_on=["package"],
                ),
                BundleComponent(id="package", package="ripgrep"),
            ]
        ),
        _cfg(),
    )
    events: list[str] = []

    def action(kind: CapabilityTargetKind) -> CapabilityTargetAction:
        def activate() -> CapabilityActivation:
            events.append(f"activate:{kind.value}")
            return CapabilityActivation(CapabilityStatus.ACTIVE, changed=True)

        return CapabilityTargetAction(
            target_kind=kind,
            preflight=lambda: events.append(f"preflight:{kind.value}"),
            activate=activate,
        )

    outcomes = graph.execute(
        [
            action(CapabilityTargetKind.FILE),
            action(CapabilityTargetKind.PACKAGE),
        ]
    )

    assert events == [
        "preflight:package",
        "preflight:file",
        "activate:package",
        "activate:file",
    ]
    assert [outcome.status for outcome in outcomes] == [
        CapabilityStatus.ACTIVE,
        CapabilityStatus.ACTIVE,
    ]


def test_executor_blocks_descendants_and_compensates_reverse_order() -> None:
    graph = build_capability_graph(
        BundleSpec(
            components=[
                BundleComponent(id="package", package="ripgrep"),
                BundleComponent(
                    id="extension", package="theme", depends_on=["package"]
                ),
                BundleComponent(
                    id="plugin", package="review", depends_on=["extension"]
                ),
            ]
        ),
        _cfg(),
    )
    events: list[str] = []

    def active_package() -> CapabilityActivation:
        events.append("activate:package")
        return CapabilityActivation(CapabilityStatus.ACTIVE, changed=True)

    def failed_extension() -> CapabilityActivation:
        events.append("activate:extension")
        return CapabilityActivation(
            CapabilityStatus.FAILED, changed=True, detail="extension failed"
        )

    actions = [
        CapabilityTargetAction(
            CapabilityTargetKind.PACKAGE,
            preflight=lambda: events.append("preflight:package"),
            activate=active_package,
        ),
        CapabilityTargetAction(
            CapabilityTargetKind.EXTENSION,
            preflight=lambda: events.append("preflight:extension"),
            activate=failed_extension,
            compensate=lambda: events.append("compensate:extension"),
        ),
        CapabilityTargetAction(
            CapabilityTargetKind.PLUGIN,
            preflight=lambda: events.append("preflight:plugin"),
            activate=lambda: pytest.fail("blocked plugin activated"),
        ),
    ]

    outcomes = graph.execute(actions)

    assert events == [
        "preflight:package",
        "preflight:extension",
        "preflight:plugin",
        "activate:package",
        "activate:extension",
        "compensate:extension",
    ]
    assert [outcome.status for outcome in outcomes] == [
        CapabilityStatus.RECOVERY_REQUIRED,
        CapabilityStatus.COMPENSATED,
        CapabilityStatus.BLOCKED,
    ]
    assert outcomes[-1].blocked_by == (CapabilityTargetKind.EXTENSION,)


def test_executor_continues_independent_branch_after_failure() -> None:
    graph = CapabilityGraph(
        (
            CapabilityNode("package", CapabilityTargetKind.PACKAGE, ()),
            CapabilityNode("dependent", CapabilityTargetKind.PLUGIN, ("package",)),
            CapabilityNode("independent", CapabilityTargetKind.FILE, ()),
        )
    )
    activated: list[CapabilityTargetKind] = []

    def action(
        kind: CapabilityTargetKind, status: CapabilityStatus
    ) -> CapabilityTargetAction:
        def activate() -> CapabilityActivation:
            activated.append(kind)
            return CapabilityActivation(status, changed=False)

        return CapabilityTargetAction(kind, lambda: None, activate)

    outcomes = graph.execute(
        (
            action(CapabilityTargetKind.PACKAGE, CapabilityStatus.FAILED),
            action(CapabilityTargetKind.PLUGIN, CapabilityStatus.ACTIVE),
            action(CapabilityTargetKind.FILE, CapabilityStatus.ACTIVE),
        )
    )

    assert activated == [CapabilityTargetKind.PACKAGE, CapabilityTargetKind.FILE]
    assert [outcome.status for outcome in outcomes] == [
        CapabilityStatus.FAILED,
        CapabilityStatus.BLOCKED,
        CapabilityStatus.ACTIVE,
    ]
    assert outcomes[1].blocked_by == (CapabilityTargetKind.PACKAGE,)


def test_executor_requires_an_exact_target_action_inventory() -> None:
    graph = build_capability_graph(
        BundleSpec(components=[BundleComponent(id="package", package="ripgrep")]),
        _cfg(),
    )
    with pytest.raises(ConfigError, match=r"missing=\['package'\]"):
        graph.execute([])


def test_executor_preflight_failure_occurs_before_every_activation() -> None:
    graph = build_capability_graph(
        BundleSpec(
            components=[
                BundleComponent(id="package", package="ripgrep"),
                BundleComponent(id="extension", package="theme"),
            ]
        ),
        _cfg(),
    )
    activated: list[CapabilityTargetKind] = []

    def fail_preflight() -> None:
        raise ConfigError("extension inventory changed")

    def activate_package() -> CapabilityActivation:
        activated.append(CapabilityTargetKind.PACKAGE)
        return CapabilityActivation(CapabilityStatus.ACTIVE, changed=True)

    actions = [
        CapabilityTargetAction(
            CapabilityTargetKind.PACKAGE,
            preflight=lambda: None,
            activate=activate_package,
        ),
        CapabilityTargetAction(
            CapabilityTargetKind.EXTENSION,
            preflight=fail_preflight,
            activate=lambda: pytest.fail("activation crossed failed preflight"),
        ),
    ]

    with pytest.raises(ConfigError, match="inventory changed"):
        graph.execute(actions)
    assert activated == []


def test_combined_graph_namespaces_bundle_local_node_ids() -> None:
    first = build_capability_graph(
        BundleSpec(components=[BundleComponent(id="tool", package="ripgrep")]),
        _cfg(),
    )
    second = build_capability_graph(
        BundleSpec(
            components=[
                BundleComponent(id="tool", package="theme"),
                BundleComponent(id="plugin", package="review", depends_on=["tool"]),
            ]
        ),
        _cfg(),
    )

    combined = combine_capability_graphs([("core", first), ("editor", second)])

    assert [node.id for node in combined.ordered()] == [
        "4:core4:tool",
        "6:editor4:tool",
        "6:editor6:plugin",
    ]
    assert combined.nodes[-1].depends_on == ("6:editor4:tool",)


def test_combined_graph_namespace_is_injective_for_dotted_ids() -> None:
    graph = CapabilityGraph((CapabilityNode("b.c", CapabilityTargetKind.PACKAGE, ()),))
    other = CapabilityGraph((CapabilityNode("c", CapabilityTargetKind.FILE, ()),))

    combined = combine_capability_graphs((("a", graph), ("a.b", other)))

    assert len({node.id for node in combined.nodes}) == 2


@pytest.mark.parametrize(
    ("nodes", "message"),
    [
        (
            (
                CapabilityNode("same", CapabilityTargetKind.FILE, ()),
                CapabilityNode("same", CapabilityTargetKind.PACKAGE, ()),
            ),
            "duplicate",
        ),
        (
            (CapabilityNode("node", CapabilityTargetKind.FILE, ("missing",)),),
            "unknown id",
        ),
        (
            (
                CapabilityNode("a", CapabilityTargetKind.FILE, ("b",)),
                CapabilityNode("b", CapabilityTargetKind.PACKAGE, ("a",)),
            ),
            "cycle",
        ),
    ],
)
def test_direct_graph_construction_rejects_invalid_shapes(
    nodes: tuple[CapabilityNode, ...], message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        CapabilityGraph(nodes)


def test_direct_node_rejects_string_target_kind() -> None:
    with pytest.raises(ConfigError, match="typed target"):
        CapabilityNode("node", "package", ())  # type: ignore[arg-type]


def test_activation_rejects_string_status() -> None:
    with pytest.raises(ValueError, match="active or failed"):
        CapabilityActivation("active", changed=False)  # type: ignore[arg-type]


def test_graph_rejects_mutable_or_non_node_inventory() -> None:
    node = CapabilityNode("node", CapabilityTargetKind.FILE, ())
    with pytest.raises(ConfigError, match="tuple of CapabilityNode"):
        CapabilityGraph([node])  # type: ignore[arg-type]
    with pytest.raises(ConfigError, match="tuple of CapabilityNode"):
        CapabilityGraph((object(),))  # type: ignore[arg-type]
