from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


Point = tuple[float, float]
MappedNode = tuple[str, str, float, float]
Segment = tuple[str, Point, Point]


@dataclass(frozen=True)
class PlannedNetPath:
    points: tuple[Point, ...]
    owner_refdes: str | None = None
    owner_pin: str | None = None


@dataclass(frozen=True)
class PlannedLabelStub:
    net_name: str
    pick_x_mm: float
    pick_y_mm: float
    label_x_mm: float
    label_y_mm: float
    owner_refdes: str | None = None
    owner_pin: str | None = None
    path_mode: str = ""


@dataclass(frozen=True)
class NetAttachmentEndpoint:
    refdes: str
    pin: str
    x_mm: float
    y_mm: float


@dataclass(frozen=True)
class NetAttachmentPath:
    mode: str
    points: tuple[Point, ...]
    owner_refdes: str | None = None
    owner_pin: str | None = None


@dataclass(frozen=True)
class NetAttachmentPlan:
    net_name: str
    normalized_name: str
    strategy: str
    power_like: bool
    endpoints: tuple[NetAttachmentEndpoint, ...]
    paths: tuple[NetAttachmentPath, ...]


@dataclass
class NetAttachmentPlanResult:
    plans: tuple[NetAttachmentPlan, ...]
    occupied_segments: list[Segment]
    pending_label_stubs: list[PlannedLabelStub]
    connected_component_refs: set[str]
    connected_pin_keys: set[tuple[str, str]]
    label_owner_collision_count: int = 0

    def as_report_dict(self) -> dict[str, Any]:
        return {
            "net_plan_count": len(self.plans),
            "occupied_segment_count": len(self.occupied_segments),
            "pending_label_stub_count": len(self.pending_label_stubs),
            "label_owner_collision_count": self.label_owner_collision_count,
            "pending_label_stubs": [
                {
                    "net_name": label.net_name,
                    "pick_point_mm": {"x": label.pick_x_mm, "y": label.pick_y_mm},
                    "label_point_mm": {"x": label.label_x_mm, "y": label.label_y_mm},
                    "owner_refdes": label.owner_refdes,
                    "owner_pin": label.owner_pin,
                    "path_mode": label.path_mode,
                }
                for label in self.pending_label_stubs
            ],
            "plans": [
                {
                    "net_name": plan.net_name,
                    "normalized_name": plan.normalized_name,
                    "strategy": plan.strategy,
                    "power_like": plan.power_like,
                    "endpoint_count": len(plan.endpoints),
                    "path_count": len(plan.paths),
                    "endpoints": [
                        {
                            "refdes": endpoint.refdes,
                            "pin": endpoint.pin,
                            "point_mm": {"x": endpoint.x_mm, "y": endpoint.y_mm},
                        }
                        for endpoint in plan.endpoints
                    ],
                    "paths": [
                        {
                            "mode": path.mode,
                            "owner_refdes": path.owner_refdes,
                            "owner_pin": path.owner_pin,
                            "points_mm": [
                                {"x": point[0], "y": point[1]}
                                for point in path.points
                            ],
                        }
                        for path in plan.paths
                    ],
                }
                for plan in self.plans
            ],
        }


def build_net_attachment_plan(
    connection_map: list[Any],
    placement_map: dict[str, tuple[float, float]],
    all_anchor_points: set[tuple[float, float]],
    resolved_anchor_by_ref_pin: dict[tuple[str, str], Any],
    should_draw_net: Callable[[str, list[MappedNode]], bool],
    should_draw_net_with_stub_labels: Callable[[str, list[MappedNode], dict[str, tuple[float, float]]], bool],
    build_stub_label_paths_for_net: Callable[
        [
            str,
            list[MappedNode],
            dict[tuple[str, str], Any],
            dict[str, tuple[float, float]],
            float,
            list[Segment],
            set[tuple[float, float]],
        ],
        list[Any],
    ],
    route_net_paths: Callable[[str, list[MappedNode], list[Segment], dict[str, tuple[float, float]], set[tuple[float, float]], bool], list[Any]],
    legacy_chain_paths_for_net: Callable[[str, list[MappedNode], list[Segment], dict[str, tuple[float, float]], set[tuple[float, float]]], list[Any]],
    normalize_power_net_name: Callable[[str], str | None],
    append_occupied_segments: Callable[[list[Segment], str, list[Point]], None],
    point_key: Callable[[Point], tuple[float, float]],
    label_spec_for_path: Callable[[list[Point]], tuple[float, float, float, float] | None],
    stub_length_mm: float,
    snap_to_default_grid: bool,
    snap_path_to_grid: Callable[[list[Point]], list[Point]],
) -> NetAttachmentPlanResult:
    plans: list[NetAttachmentPlan] = []
    pending_label_stubs: list[PlannedLabelStub] = []
    occupied_segments: list[Segment] = []
    connected_component_refs: set[str] = set()
    connected_pin_keys: set[tuple[str, str]] = set()
    label_owner_collision_count = 0

    for connection in connection_map:
        net_name = str(getattr(connection, "net_name", "") or "").strip()
        mapped_nodes: list[MappedNode] = [
            (
                str(getattr(node, "refdes", "")),
                str(getattr(node, "pin", "")),
                float(getattr(getattr(node, "anchor", None), "x_mm", 0.0)),
                float(getattr(getattr(node, "anchor", None), "y_mm", 0.0)),
            )
            for node in list(getattr(connection, "nodes", []) or [])
        ]
        mapped_nodes = [item for item in mapped_nodes if item[0] and item[1]]
        if not mapped_nodes:
            continue
        if not should_draw_net(net_name, mapped_nodes):
            continue

        current_net_points = {point_key((x, y)) for _, _, x, y in mapped_nodes}
        forbidden_points = all_anchor_points - current_net_points

        draw_as_stub_labels = should_draw_net_with_stub_labels(
            net_name,
            mapped_nodes,
            placement_map,
        )
        strategy = "stub_labels" if draw_as_stub_labels else "routed"
        path_mode = "stub" if draw_as_stub_labels else "routed"
        fallback_to_stub_labels = False
        if draw_as_stub_labels:
            net_paths = build_stub_label_paths_for_net(
                net_name,
                mapped_nodes,
                resolved_anchor_by_ref_pin,
                placement_map,
                stub_length_mm,
                occupied_segments,
                forbidden_points,
            )
        else:
            net_paths = route_net_paths(
                net_name,
                mapped_nodes,
                occupied_segments,
                placement_map,
                forbidden_points,
                False,
            )
            if not net_paths:
                net_paths = legacy_chain_paths_for_net(
                    net_name,
                    mapped_nodes,
                    occupied_segments,
                    placement_map,
                    forbidden_points,
                )
                if net_paths:
                    strategy = "legacy_chain"
                    path_mode = "legacy_chain"
            if not net_paths:
                net_paths = build_stub_label_paths_for_net(
                    net_name,
                    mapped_nodes,
                    resolved_anchor_by_ref_pin,
                    placement_map,
                    stub_length_mm,
                    occupied_segments,
                    forbidden_points,
                )
                if net_paths:
                    strategy = "fallback_stub_labels"
                    path_mode = "stub_fallback"
                    fallback_to_stub_labels = True
                else:
                    strategy = "unroutable_no_paths"
                    path_mode = "unroutable"

        power_key = normalize_power_net_name(net_name)
        path_endpoint_refs = _path_endpoint_refdes_index(mapped_nodes, point_key)
        path_items: list[NetAttachmentPath] = []
        for raw_path in net_paths:
            candidate = _coerce_planned_path(raw_path)
            path = list(candidate.points)
            if len(path) < 2:
                continue
            if snap_to_default_grid:
                path = snap_path_to_grid(path)
            append_occupied_segments(occupied_segments, net_name, path)
            path_items.append(
                NetAttachmentPath(
                    mode=path_mode,
                    points=tuple(path),
                    owner_refdes=candidate.owner_refdes,
                    owner_pin=candidate.owner_pin,
                )
            )
            if draw_as_stub_labels or fallback_to_stub_labels or power_key:
                label_spec = label_spec_for_path(path)
                if label_spec is not None:
                    owner_refdes, owner_pin = _label_owner_for_path(
                        candidate,
                        path,
                        path_endpoint_refs,
                        point_key,
                    )
                    pending_label_stubs.append(
                        PlannedLabelStub(
                            net_name=net_name,
                            pick_x_mm=label_spec[0],
                            pick_y_mm=label_spec[1],
                            label_x_mm=label_spec[2],
                            label_y_mm=label_spec[3],
                            owner_refdes=owner_refdes,
                            owner_pin=owner_pin,
                            path_mode=path_mode,
                        )
                    )

        if path_items:
            connected_component_refs.update(ref for ref, _, _, _ in mapped_nodes)
            connected_pin_keys.update((ref, pin) for ref, pin, _, _ in mapped_nodes)

        endpoints = tuple(
            NetAttachmentEndpoint(refdes=ref, pin=pin, x_mm=x_mm, y_mm=y_mm)
            for ref, pin, x_mm, y_mm in sorted(mapped_nodes, key=lambda item: (item[0], _pin_sort_key(item[1])))
        )
        plans.append(
            NetAttachmentPlan(
                net_name=net_name,
                normalized_name=_normalize_net_name(net_name),
                strategy=strategy,
                power_like=bool(power_key),
                endpoints=endpoints,
                paths=tuple(path_items),
            )
        )

    return NetAttachmentPlanResult(
        plans=tuple(plans),
        occupied_segments=occupied_segments,
        pending_label_stubs=pending_label_stubs,
        connected_component_refs=connected_component_refs,
        connected_pin_keys=connected_pin_keys,
        label_owner_collision_count=label_owner_collision_count,
    )


def _normalize_net_name(name: str) -> str:
    return "".join(ch for ch in str(name or "").upper() if ch.isalnum() or ch == "_")


def _pin_sort_key(pin_id: str) -> tuple[int, str]:
    token = str(pin_id or "").strip()
    if token.isdigit():
        return (0, f"{int(token):09d}")
    return (1, token.upper())


def _coerce_planned_path(raw_path: Any) -> PlannedNetPath:
    if isinstance(raw_path, PlannedNetPath):
        return raw_path
    points = tuple(tuple(point) for point in list(raw_path or []))
    return PlannedNetPath(points=points)


def _path_endpoint_refdes_index(
    mapped_nodes: list[MappedNode],
    point_key: Callable[[Point], tuple[float, float]],
) -> dict[tuple[float, float], set[tuple[str, str]]]:
    out: dict[tuple[float, float], set[tuple[str, str]]] = {}
    for refdes, pin, x_mm, y_mm in mapped_nodes:
        key = point_key((x_mm, y_mm))
        refs = out.setdefault(key, set())
        refs.add((refdes, pin))
    return out


def _label_owner_for_path(
    candidate: PlannedNetPath,
    path: list[Point],
    endpoint_ref_index: dict[tuple[float, float], set[tuple[str, str]]],
    point_key: Callable[[Point], tuple[float, float]],
) -> tuple[str | None, str | None]:
    if candidate.owner_refdes:
        return candidate.owner_refdes, candidate.owner_pin
    if len(path) < 2:
        return (None, None)
    start_matches = endpoint_ref_index.get(point_key(path[0]), set())
    if len(start_matches) == 1:
        return next(iter(start_matches))
    end_matches = endpoint_ref_index.get(point_key(path[-1]), set())
    if len(end_matches) == 1:
        return next(iter(end_matches))
    return (None, None)
