from __future__ import annotations

from dataclasses import dataclass

from easyeda2fusion.builders.schematic_connectivity import build_board_derived_net_connection_map
from easyeda2fusion.builders.schematic_geometry import build_schematic_geometry_maps
from easyeda2fusion.builders.schematic_netplan import (
    NetAttachmentPath,
    NetAttachmentPlan,
    PlannedNetPath,
    build_net_attachment_plan,
)
from easyeda2fusion.builders.schematic_placement import build_board_derived_placement_map
from easyeda2fusion.emitters.schematic_draw import emit_net_attachment_lines
from easyeda2fusion.model import Component, Net, NetNode, Package, Pad, Point, Project, Side, SourceFormat, Symbol, SymbolPin
from easyeda2fusion.reports.schematic_pipeline import write_schematic_pipeline_reports


@dataclass(frozen=True)
class _Anchor:
    x_mm: float
    y_mm: float
    outward_dx: float
    outward_dy: float


@dataclass(frozen=True)
class _Node:
    refdes: str
    pin: str
    anchor: _Anchor


@dataclass(frozen=True)
class _Connection:
    net_name: str
    nodes: tuple[_Node, ...]


def test_symbol_geometry_map_supports_generated_and_external_symbols() -> None:
    project = Project(
        project_id="p_geom_mix",
        name="p_geom_mix",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        symbols=[
            Symbol(
                symbol_id="SYM_U1",
                name="SYM_U1",
                pins=[SymbolPin(pin_number="1", pin_name="IN", at=Point(-2.0, 0.0))],
            )
        ],
        components=[
            Component(
                refdes="U1",
                value="",
                source_name="U",
                symbol_id="SYM_U1",
                device_id="easyeda_generated:DEV_U1",
                package_id="PKG",
                at=Point(0.0, 0.0),
            ),
            Component(
                refdes="U2",
                value="",
                source_name="U",
                symbol_id=None,
                device_id="samacsys:MAX98357",
                package_id="PKG",
                at=Point(40.0, 0.0),
                side=Side.TOP,
            ),
        ],
        packages=[
            Package(
                package_id="PKG",
                name="PKG",
                pads=[Pad(pad_number="1", at=Point(0.0, 0.0), shape="rect", width_mm=1.0, height_mm=1.0)],
            )
        ],
    )
    refdes_map = {"U1": "U1", "U2": "U2"}
    placement_map = {"U1": (20.0, 20.0), "U2": (45.0, 20.0)}
    anchor_map = {
        "U1": {"1": _Anchor(18.0, 20.0, -1.0, 0.0)},
        "U2": {"1": _Anchor(43.0, 20.0, -1.0, 0.0)},
    }
    external_local_pin_map = {
        "U2": {"1": _Anchor(-2.0, 0.0, -1.0, 0.0)},
    }

    maps = build_schematic_geometry_maps(
        project=project,
        refdes_map=refdes_map,
        placement_map=placement_map,
        anchor_map=anchor_map,
        external_local_pin_map_by_ref=external_local_pin_map,
        resolve_symbol_origin=lambda _symbol: (0.0, 0.0),
        resolve_component_rotation=lambda _component: 0.0,
    )

    assert ("U1", "1") in maps.placed_pin_anchors
    assert ("U2", "1") in maps.placed_pin_anchors
    assert maps.symbol_origins["U1"].symbol_source_type == "generated"
    assert maps.symbol_origins["U2"].symbol_source_type == "external_library"


def test_symbol_origin_map_uses_schematic_placement_origin() -> None:
    project = Project(
        project_id="p_origin_map",
        name="p_origin_map",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        symbols=[Symbol(symbol_id="SYM", name="SYM", pins=[SymbolPin(pin_number="1", pin_name="1", at=Point(0.0, 0.0))])],
        components=[
            Component(
                refdes="U1",
                value="",
                source_name="U",
                symbol_id="SYM",
                device_id="easyeda_generated:DEV_U1",
                package_id="PKG",
                at=Point(1.0, 2.0),
            )
        ],
    )
    maps = build_schematic_geometry_maps(
        project=project,
        refdes_map={"U1": "U1"},
        placement_map={"U1": (100.0, 200.0)},
        anchor_map={"U1": {"1": _Anchor(100.0, 200.0, -1.0, 0.0)}},
        external_local_pin_map_by_ref={},
        resolve_symbol_origin=lambda _symbol: (0.0, 0.0),
        resolve_component_rotation=lambda _component: 90.0,
    )
    origin = maps.symbol_origins["U1"]
    assert origin.schematic_origin_x_mm == 100.0
    assert origin.schematic_origin_y_mm == 200.0
    assert origin.rotation_deg == 90.0


def test_board_derived_net_connection_map_classifies_power_ground_signal() -> None:
    connections = [
        _Connection(
            net_name="GND",
            nodes=(
                _Node("U1", "1", _Anchor(10.0, 10.0, 0.0, -1.0)),
                _Node("U2", "1", _Anchor(20.0, 10.0, 0.0, -1.0)),
            ),
        ),
        _Connection(
            net_name="VCC",
            nodes=(
                _Node("U1", "2", _Anchor(10.0, 20.0, 0.0, 1.0)),
                _Node("U2", "2", _Anchor(20.0, 20.0, 0.0, 1.0)),
            ),
        ),
        _Connection(
            net_name="SCL5",
            nodes=(
                _Node("U1", "3", _Anchor(10.0, 30.0, 1.0, 0.0)),
                _Node("U2", "3", _Anchor(20.0, 30.0, -1.0, 0.0)),
            ),
        ),
    ]
    net_map = build_board_derived_net_connection_map(connections)
    kinds = {item.net_name: item.net_kind for item in net_map.nets}
    assert kinds["GND"] == "ground"
    assert kinds["VCC"] == "power"
    assert kinds["SCL5"] == "signal"


def test_board_derived_placement_map_uses_blocks_and_neighbors() -> None:
    project = Project(
        project_id="p_place",
        name="p_place",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        components=[
            Component(refdes="U1", value="", source_name="U", device_id="easyeda_generated:DEV_U1", package_id="PKG", at=Point(10.0, 10.0)),
            Component(refdes="C1", value="", source_name="C", device_id="easyeda_generated:DEV_C1", package_id="PKG", at=Point(12.0, 10.0)),
        ],
        nets=[Net(name="3V3", nodes=[NetNode(refdes="U1", pin="1"), NetNode(refdes="C1", pin="1")])],
    )
    placement = build_board_derived_placement_map(
        project=project,
        refdes_map={"U1": "U1", "C1": "C1"},
        placement_map={"U1": (100.0, 100.0), "C1": (110.0, 100.0)},
        effective_nets=project.nets,
        layout_metadata={
            "mode": "board",
            "blocks": [
                {"id": "power_0", "kind": "power", "components": ["U1", "C1"]},
            ],
        },
    )
    entries = {item.refdes: item for item in placement.entries}
    assert entries["U1"].block_id == "power_0"
    assert entries["U1"].block_kind == "power"
    assert "C1" in entries["U1"].neighbor_refs


def test_net_attachment_plan_is_deterministic() -> None:
    connection_map = [
        _Connection(
            net_name="SIG",
            nodes=(
                _Node("U1", "1", _Anchor(10.0, 10.0, -1.0, 0.0)),
                _Node("U2", "1", _Anchor(30.0, 20.0, 1.0, 0.0)),
            ),
        )
    ]

    def _should_draw(_name, _nodes):
        return True

    def _should_stub(_name, _nodes, _placement):
        return False

    def _build_stub(_net_name, _nodes, _anchors, _placement, _stub_len, _occupied, _forbidden):
        return []

    def _route(_name, nodes, _occupied, _placement, _forbidden, _allow_dense):
        (x1, y1) = (nodes[0][2], nodes[0][3])
        (x2, y2) = (nodes[1][2], nodes[1][3])
        mid_x = (x1 + x2) / 2.0
        return [[(x1, y1), (mid_x, y1), (mid_x, y2), (x2, y2)]]

    def _legacy(_name, _nodes, _occupied, _placement, _forbidden):
        return []

    def _norm_power(_name):
        return None

    def _append(occupied, name, path):
        for idx in range(len(path) - 1):
            occupied.append((name, path[idx], path[idx + 1]))

    def _point_key(point):
        return (round(point[0], 6), round(point[1], 6))

    def _label_spec(path):
        start = path[0]
        end = path[-1]
        return (start[0], start[1], end[0], end[1])

    def _snap(path):
        return path

    kwargs = dict(
        connection_map=connection_map,
        placement_map={"U1": (10.0, 10.0), "U2": (30.0, 20.0)},
        all_anchor_points={(10.0, 10.0), (30.0, 20.0)},
        resolved_anchor_by_ref_pin={},
        should_draw_net=_should_draw,
        should_draw_net_with_stub_labels=_should_stub,
        build_stub_label_paths_for_net=_build_stub,
        route_net_paths=_route,
        legacy_chain_paths_for_net=_legacy,
        normalize_power_net_name=_norm_power,
        append_occupied_segments=_append,
        point_key=_point_key,
        label_spec_for_path=_label_spec,
        stub_length_mm=1.27,
        snap_to_default_grid=False,
        snap_path_to_grid=_snap,
    )
    plan_a = build_net_attachment_plan(**kwargs)
    plan_b = build_net_attachment_plan(**kwargs)
    assert plan_a.as_report_dict() == plan_b.as_report_dict()


def test_net_attachment_plan_falls_back_to_stub_labels_when_routing_fails() -> None:
    connection_map = [
        _Connection(
            net_name="SIG",
            nodes=(
                _Node("U1", "1", _Anchor(10.0, 10.0, -1.0, 0.0)),
                _Node("U2", "1", _Anchor(20.0, 10.0, 1.0, 0.0)),
            ),
        )
    ]

    def _should_draw(_name, _nodes):
        return True

    def _should_stub(_name, _nodes, _placement):
        return False

    def _build_stub(_net_name, nodes, _anchors, _placement, stub_len, _occupied, _forbidden):
        out: list[list[tuple[float, float]]] = []
        for _ref, _pin, x_mm, y_mm in nodes:
            out.append([(x_mm, y_mm), (x_mm + stub_len, y_mm)])
        return out

    def _route(_name, _nodes, _occupied, _placement, _forbidden, _allow_dense):
        return []

    def _legacy(_name, _nodes, _occupied, _placement, _forbidden):
        return []

    def _norm_power(_name):
        return None

    def _append(occupied, name, path):
        for idx in range(len(path) - 1):
            occupied.append((name, path[idx], path[idx + 1]))

    def _point_key(point):
        return (round(point[0], 6), round(point[1], 6))

    def _label_spec(path):
        end = path[-1]
        return (end[0], end[1], end[0], end[1])

    def _snap(path):
        return path

    plan = build_net_attachment_plan(
        connection_map=connection_map,
        placement_map={"U1": (10.0, 10.0), "U2": (20.0, 10.0)},
        all_anchor_points={(10.0, 10.0), (20.0, 10.0)},
        resolved_anchor_by_ref_pin={},
        should_draw_net=_should_draw,
        should_draw_net_with_stub_labels=_should_stub,
        build_stub_label_paths_for_net=_build_stub,
        route_net_paths=_route,
        legacy_chain_paths_for_net=_legacy,
        normalize_power_net_name=_norm_power,
        append_occupied_segments=_append,
        point_key=_point_key,
        label_spec_for_path=_label_spec,
        stub_length_mm=1.27,
        snap_to_default_grid=False,
        snap_path_to_grid=_snap,
    )

    assert len(plan.plans) == 1
    net_plan = plan.plans[0]
    assert net_plan.strategy == "fallback_stub_labels"
    assert len(net_plan.paths) == 2
    assert {item.mode for item in net_plan.paths} == {"stub_fallback"}
    assert len(plan.pending_label_stubs) == 2
    assert plan.connected_component_refs == {"U1", "U2"}
    assert plan.connected_pin_keys == {("U1", "1"), ("U2", "1")}


def test_net_attachment_plan_keeps_per_stub_labels_for_same_net_on_one_component() -> None:
    connection_map = [
        _Connection(
            net_name="3V3",
            nodes=(
                _Node("U1", "1", _Anchor(10.0, 10.0, -1.0, 0.0)),
                _Node("U1", "2", _Anchor(10.0, 12.54, -1.0, 0.0)),
                _Node("U2", "1", _Anchor(20.0, 10.0, 1.0, 0.0)),
            ),
        )
    ]

    def _should_draw(_name, _nodes):
        return True

    def _should_stub(_name, _nodes, _placement):
        return True

    def _build_stub(_net_name, nodes, _anchors, _placement, stub_len, _occupied, _forbidden):
        out: list[list[tuple[float, float]]] = []
        for _ref, _pin, x_mm, y_mm in nodes:
            out.append([(x_mm, y_mm), (x_mm + stub_len, y_mm)])
        return out

    def _route(_name, _nodes, _occupied, _placement, _forbidden, _allow_dense):
        return []

    def _legacy(_name, _nodes, _occupied, _placement, _forbidden):
        return []

    def _norm_power(name):
        return "3V3" if name == "3V3" else None

    def _append(occupied, name, path):
        for idx in range(len(path) - 1):
            occupied.append((name, path[idx], path[idx + 1]))

    def _point_key(point):
        return (round(point[0], 6), round(point[1], 6))

    def _label_spec(path):
        end = path[-1]
        return (end[0], end[1], end[0], end[1])

    def _snap(path):
        return path

    plan = build_net_attachment_plan(
        connection_map=connection_map,
        placement_map={"U1": (10.0, 11.27), "U2": (20.0, 10.0)},
        all_anchor_points={(10.0, 10.0), (10.0, 12.54), (20.0, 10.0)},
        resolved_anchor_by_ref_pin={},
        should_draw_net=_should_draw,
        should_draw_net_with_stub_labels=_should_stub,
        build_stub_label_paths_for_net=_build_stub,
        route_net_paths=_route,
        legacy_chain_paths_for_net=_legacy,
        normalize_power_net_name=_norm_power,
        append_occupied_segments=_append,
        point_key=_point_key,
        label_spec_for_path=_label_spec,
        stub_length_mm=1.27,
        snap_to_default_grid=False,
        snap_path_to_grid=_snap,
    )

    assert len(plan.plans) == 1
    net_plan = plan.plans[0]
    assert net_plan.strategy == "stub_labels"
    assert len(net_plan.paths) == 3
    assert len(plan.pending_label_stubs) == 3
    owner_pin_pairs = {(item.owner_refdes, item.owner_pin) for item in plan.pending_label_stubs}
    assert owner_pin_pairs == {("U1", "1"), ("U1", "2"), ("U2", "1")}


def test_net_attachment_plan_prefers_explicit_path_owner_over_endpoint_sorting() -> None:
    connection_map = [
        _Connection(
            net_name="3V3",
            nodes=(
                _Node("Z1", "1", _Anchor(30.0, 10.0, 1.0, 0.0)),
                _Node("A1", "1", _Anchor(10.0, 10.0, -1.0, 0.0)),
            ),
        )
    ]

    def _should_draw(_name, _nodes):
        return True

    def _should_stub(_name, _nodes, _placement):
        return False

    def _build_stub(_net_name, _nodes, _anchors, _placement, _stub_len, _occupied, _forbidden):
        return []

    def _route(_name, nodes, _occupied, _placement, _forbidden, _allow_dense):
        start = (nodes[0][2], nodes[0][3])
        end = (nodes[1][2], nodes[1][3])
        return [PlannedNetPath(points=(start, (20.0, 10.0), end), owner_refdes="Z1", owner_pin="1")]

    def _legacy(_name, _nodes, _occupied, _placement, _forbidden):
        return []

    def _norm_power(name):
        return "3V3" if name == "3V3" else None

    def _append(occupied, name, path):
        for idx in range(len(path) - 1):
            occupied.append((name, path[idx], path[idx + 1]))

    def _point_key(point):
        return (round(point[0], 6), round(point[1], 6))

    def _label_spec(path):
        end = path[-1]
        return (end[0], end[1], end[0], end[1])

    def _snap(path):
        return path

    plan = build_net_attachment_plan(
        connection_map=connection_map,
        placement_map={"Z1": (30.0, 10.0), "A1": (10.0, 10.0)},
        all_anchor_points={(30.0, 10.0), (10.0, 10.0)},
        resolved_anchor_by_ref_pin={},
        should_draw_net=_should_draw,
        should_draw_net_with_stub_labels=_should_stub,
        build_stub_label_paths_for_net=_build_stub,
        route_net_paths=_route,
        legacy_chain_paths_for_net=_legacy,
        normalize_power_net_name=_norm_power,
        append_occupied_segments=_append,
        point_key=_point_key,
        label_spec_for_path=_label_spec,
        stub_length_mm=1.27,
        snap_to_default_grid=False,
        snap_path_to_grid=_snap,
    )

    assert len(plan.pending_label_stubs) == 1
    assert plan.pending_label_stubs[0].owner_refdes == "Z1"
    assert plan.plans[0].paths[0].owner_refdes == "Z1"


def test_net_attachment_plan_does_not_mark_connections_when_no_paths_emit() -> None:
    connection_map = [
        _Connection(
            net_name="SIG",
            nodes=(
                _Node("U1", "1", _Anchor(10.0, 10.0, -1.0, 0.0)),
                _Node("U2", "1", _Anchor(20.0, 10.0, 1.0, 0.0)),
            ),
        )
    ]

    def _should_draw(_name, _nodes):
        return True

    def _should_stub(_name, _nodes, _placement):
        return False

    def _build_stub(_net_name, _nodes, _anchors, _placement, _stub_len, _occupied, _forbidden):
        return []

    def _route(_name, _nodes, _occupied, _placement, _forbidden, _allow_dense):
        return []

    def _legacy(_name, _nodes, _occupied, _placement, _forbidden):
        return []

    def _norm_power(_name):
        return None

    def _append(occupied, name, path):
        for idx in range(len(path) - 1):
            occupied.append((name, path[idx], path[idx + 1]))

    def _point_key(point):
        return (round(point[0], 6), round(point[1], 6))

    def _label_spec(path):
        end = path[-1]
        return (end[0], end[1], end[0], end[1])

    def _snap(path):
        return path

    plan = build_net_attachment_plan(
        connection_map=connection_map,
        placement_map={"U1": (10.0, 10.0), "U2": (20.0, 10.0)},
        all_anchor_points={(10.0, 10.0), (20.0, 10.0)},
        resolved_anchor_by_ref_pin={},
        should_draw_net=_should_draw,
        should_draw_net_with_stub_labels=_should_stub,
        build_stub_label_paths_for_net=_build_stub,
        route_net_paths=_route,
        legacy_chain_paths_for_net=_legacy,
        normalize_power_net_name=_norm_power,
        append_occupied_segments=_append,
        point_key=_point_key,
        label_spec_for_path=_label_spec,
        stub_length_mm=1.27,
        snap_to_default_grid=False,
        snap_path_to_grid=_snap,
    )

    assert len(plan.plans) == 1
    net_plan = plan.plans[0]
    assert net_plan.strategy == "unroutable_no_paths"
    assert len(net_plan.paths) == 0
    assert plan.connected_component_refs == set()
    assert plan.connected_pin_keys == set()


def test_wire_rendering_uses_exact_planned_endpoints() -> None:
    plan = NetAttachmentPlan(
        net_name="SIG",
        normalized_name="SIG",
        strategy="routed",
        power_like=False,
        endpoints=(),
        paths=(NetAttachmentPath(mode="routed", points=((10.0, 10.0), (20.0, 10.0))),),
    )
    lines = emit_net_attachment_lines(
        plans=(plan,),
        use_inch_output=False,
        quote_token=lambda token: f"'{token}'",
        coord_for_output=lambda value, _inch: value,
    )
    assert lines == ["NET 'SIG' (10.0000 10.0000) (20.0000 10.0000);"]


def test_schematic_pipeline_report_writer_emits_expected_files(tmp_path) -> None:
    project = Project(
        project_id="p_reports",
        name="p_reports",
        source_format=SourceFormat.EASYEDA_STD,
        input_files=[],
        metadata={
            "schematic_symbol_geometry_map": {"symbol_count": 1},
            "schematic_symbol_origin_map": {"placed_symbol_count": 1},
            "schematic_board_net_connection_map": {"net_count": 1},
            "schematic_board_placement_map": {"component_count": 1},
            "schematic_net_attachment_plan": {"net_plan_count": 1},
            "schematic_pipeline_validation_summary": {"valid": True},
        },
    )
    paths = write_schematic_pipeline_reports(project, tmp_path)
    assert "symbol_geometry_map_json" in paths
    assert "symbol_origin_map_json" in paths
    assert "board_net_connection_map_json" in paths
    assert "board_placement_map_json" in paths
    assert "net_attachment_plan_json" in paths
    assert "pipeline_validation_summary_json" in paths
    for path in paths.values():
        assert path.exists()
