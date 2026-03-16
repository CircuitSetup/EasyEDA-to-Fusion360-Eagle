from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import math
import re
import bisect
from collections import defaultdict
from typing import Any, Iterable
import xml.etree.ElementTree as ET

from easyeda2fusion.builders.component_identity import (
    build_refdes_map as _shared_build_refdes_map,
    component_instance_key as _shared_component_instance_key,
    resolve_component_refdes as _shared_resolve_component_refdes,
    sanitize_refdes as _shared_sanitize_refdes,
)
from easyeda2fusion.builders.net_aliases import project_track_net_aliases as _project_track_net_aliases
from easyeda2fusion.builders.package_utils import (
    canonicalize_two_pin_quarter_turn as _canonicalize_two_pin_quarter_turn,
    component_is_resistor as _component_is_resistor,
    is_adjustable_resistor_package as _is_adjustable_resistor_package,
    package_lookup as _shared_package_lookup,
    package_pin_count as _package_pin_count,
    resolve_component_package as _resolve_component_package_for_rotation,
    valid_pins_by_ref as _shared_valid_pins_by_ref,
)
from easyeda2fusion.model import Net, NetNode, Project, Severity, SourceFormat, project_event
from easyeda2fusion.builders.schematic_connectivity import build_board_derived_net_connection_map
from easyeda2fusion.builders.schematic_geometry import build_schematic_geometry_maps
from easyeda2fusion.builders.schematic_netplan import PlannedNetPath, build_net_attachment_plan
from easyeda2fusion.builders.schematic_placement import build_board_derived_placement_map
from easyeda2fusion.emitters.schematic_draw import emit_net_attachment_lines
from easyeda2fusion.utils.xml import parse_xml_root_with_entity_sanitization

_SCHEMATIC_DEFAULT_GRID_MM = 1.27
_MM_PER_INCH = 25.4


@dataclass(frozen=True)
class _ResolvedPinAnchor:
    x_mm: float
    y_mm: float
    outward_dx: float
    outward_dy: float


@dataclass(frozen=True)
class _CanonicalPinLocal:
    x_mm: float
    y_mm: float
    outward_dx: float
    outward_dy: float


@dataclass(frozen=True)
class _NetConnectionNode:
    refdes: str
    pin: str
    anchor: _ResolvedPinAnchor


@dataclass(frozen=True)
class _NetConnection:
    net_name: str
    nodes: tuple[_NetConnectionNode, ...]


class SchematicReconstructionBuilder:
    """Builds EAGLE script command lines for schematic reconstruction."""

    def build_commands(
        self,
        project: Project,
        library_paths: dict[str, str] | None = None,
        layout_mode: str | None = None,
    ) -> list[str]:
        snap_to_default_grid = bool(project.metadata.get("schematic_snap_to_default_grid"))
        use_inch_output = snap_to_default_grid
        lines: list[str] = ["SET WIRE_BEND 2;"]
        if use_inch_output:
            lines.insert(0, "GRID INCH 0.1 ON;")
        else:
            lines.insert(0, "GRID MM 0.1 ON;")

        refdes_map = _build_refdes_map(project)
        valid_pins_by_ref = _valid_pins_by_ref(project)
        package_lookup = _package_lookup(project)

        net_alias: dict[str, str] = {}
        if project.board is not None:
            net_alias = _project_track_net_aliases(project)
        effective_nets = _coalesced_nets(project, net_alias, refdes_map, valid_pins_by_ref)

        resolved_layout_mode = _resolve_schematic_layout_mode(
            layout_mode or str(project.metadata.get("schematic_layout_mode") or "")
        )
        project.metadata["schematic_layout_mode"] = resolved_layout_mode
        project.metadata["schematic_connection_map_routing"] = True
        resolved_library_paths = _normalized_library_paths(library_paths or {})
        component_radii = _component_layout_radii_mm(
            project=project,
            refdes_map=refdes_map,
            library_paths=resolved_library_paths,
        )
        placement_map, layout_metadata = _auto_layout_positions(
            project,
            refdes_map,
            layout_mode=resolved_layout_mode,
            effective_nets=effective_nets,
        )
        project.metadata["schematic_layout_blocks"] = layout_metadata.get("blocks", [])
        placement_map = _resolve_component_overlaps(
            project,
            refdes_map,
            placement_map,
            component_radii_mm=component_radii,
        )
        if snap_to_default_grid:
            placement_map = _snap_placement_map_to_grid(placement_map)
        placement_map = _translate_placement_map_into_visible_window(
            placement_map=placement_map,
            snap_to_default_grid=snap_to_default_grid,
        )
        placeable_refs = {
            _resolve_component_refdes(component, refdes_map)
            for component in project.components
            if component.device_id
        }
        placed_refs: set[str] = set()
        anchor_map = _build_anchor_map(
            project=project,
            refdes_map=refdes_map,
            placement_map=placement_map,
            source_format=project.source_format,
            package_lookup=package_lookup,
        )
        external_anchor_map = _build_external_anchor_map(
            project=project,
            refdes_map=refdes_map,
            placement_map=placement_map,
            library_paths=resolved_library_paths,
            source_format=project.source_format,
            package_lookup=package_lookup,
        )
        for refdes, pin_map in external_anchor_map.items():
            anchor_map.setdefault(refdes, {}).update(pin_map)
        all_anchor_points = _all_anchor_points(anchor_map)

        resolved_anchor_by_ref_pin: dict[tuple[str, str], _ResolvedPinAnchor] = {}
        unresolved_pin_anchor_count = 0
        unresolved_pin_anchor_items: list[dict[str, str]] = []
        unresolved_pin_anchor_seen: set[tuple[str, str, str]] = set()
        for net in effective_nets:
            for node in net.nodes:
                ref = refdes_map.get(node.refdes, _sanitize_refdes(node.refdes))
                if ref not in placeable_refs:
                    continue
                pin = str(node.pin).strip()
                if not pin:
                    continue
                valid_pins = valid_pins_by_ref.get(ref, set())
                if valid_pins and pin not in valid_pins:
                    continue
                key = (ref, pin)
                if key in resolved_anchor_by_ref_pin:
                    continue
                ref_anchors = anchor_map.get(ref, {})
                if pin in ref_anchors:
                    anchor = ref_anchors[pin]
                else:
                    unresolved_pin_anchor_count += 1
                    unresolved_key = (ref, pin, str(net.name))
                    if unresolved_key not in unresolved_pin_anchor_seen:
                        unresolved_pin_anchor_seen.add(unresolved_key)
                        unresolved_pin_anchor_items.append(
                            {
                                "refdes": ref,
                                "pin": pin,
                                "net": str(net.name),
                                "reason": "missing_symbol_pin_anchor",
                            }
                        )
                    x, y = _net_anchor(placement_map, anchor_map, ref, pin)
                    anchor = _fallback_anchor_from_component_center(placement_map, ref, x, y)
                resolved_anchor_by_ref_pin[key] = anchor
                all_anchor_points.add(_point_key((anchor.x_mm, anchor.y_mm)))

        # Stage 1/2: symbol resolution + canonical geometry/origin maps.
        external_local_pin_map = _build_external_local_pin_map(
            project=project,
            refdes_map=refdes_map,
            library_paths=resolved_library_paths,
        )
        geometry_maps = build_schematic_geometry_maps(
            project=project,
            refdes_map=refdes_map,
            placement_map=placement_map,
            anchor_map=anchor_map,
            external_local_pin_map_by_ref=external_local_pin_map,
            resolve_symbol_origin=_symbol_origin_mm,
            resolve_component_rotation=lambda component: _schematic_component_rotation_deg(
                component=component,
                source_format=project.source_format,
                package=_resolve_component_package_for_rotation(component, package_lookup),
            ),
        )
        project.metadata["schematic_symbol_geometry_map"] = geometry_maps.symbol_geometry_report()
        project.metadata["schematic_symbol_origin_map"] = geometry_maps.symbol_origin_report()

        # Stage 4: board-derived schematic placement map (board-locality guided).
        placement_stage = build_board_derived_placement_map(
            project=project,
            refdes_map=refdes_map,
            placement_map=placement_map,
            effective_nets=effective_nets,
            layout_metadata=layout_metadata,
        )
        project.metadata["schematic_board_placement_map"] = placement_stage.as_report_dict()

        # Optional cleanup for reruns can be enabled via metadata flag, but keep
        # disabled by default to avoid noisy "Invalid part" errors on clean imports.
        if bool(project.metadata.get("schematic_delete_existing_parts")):
            for component in project.components:
                safe_refdes = _resolve_component_refdes(component, refdes_map)
                lines.append(f"DELETE {safe_refdes};")

        placement_commands_by_ref: dict[str, str] = {}
        orientation_records_by_ref: dict[str, dict[str, Any]] = {}
        placed_instance_records: list[dict[str, str]] = []
        for ordinal, component in enumerate(project.components, start=1):
            if component.device_id:
                safe_refdes = _resolve_component_refdes(component, refdes_map)
                placed_refs.add(safe_refdes)
                script_device_token = _script_device_token(
                    component.device_id,
                    resolved_library_paths,
                )
                at_x, at_y = placement_map.get(
                    safe_refdes,
                    (component.at.x_mm, component.at.y_mm),
                )
                add_x, add_y = _point_for_schematic_output((at_x, at_y), use_inch_output)
                add_rotation = _schematic_component_rotation_token(
                    component=component,
                    source_format=project.source_format,
                    package=_resolve_component_package_for_rotation(component, package_lookup),
                )
                add_command = (
                    f"ADD {_quote_token(script_device_token)} {_add_part_name_token(safe_refdes)} "
                    f"{add_rotation} ({add_x:.4f} {add_y:.4f});"
                )
                lines.append(add_command)
                placement_commands_by_ref[safe_refdes] = add_command
                orientation_records_by_ref[safe_refdes] = {
                    "rotation_token": add_rotation,
                    "rotation_deg": _snapped_schematic_rotation_deg(
                        _schematic_component_rotation_deg(
                            component=component,
                            source_format=project.source_format,
                            package=_resolve_component_package_for_rotation(component, package_lookup),
                        )
                    ),
                    "schematic_origin_mm": {"x": float(at_x), "y": float(at_y)},
                }
                placed_instance_records.append(
                    {
                        "source_refdes": str(component.refdes or ""),
                        "source_instance_id": str(getattr(component, "source_instance_id", "") or ""),
                        "source_component_key": _component_refdes_key(component, ordinal),
                        "emitted_refdes": safe_refdes,
                    }
                )
                value_text = _component_value_for_schematic(component)
                if value_text:
                    lines.append(
                        f"VALUE {_add_part_name_token(safe_refdes)} {_quote_token(value_text)};"
                    )
        project.metadata["schematic_instance_placement_commands"] = placement_commands_by_ref
        project.metadata["schematic_instance_orientation_records"] = orientation_records_by_ref
        project.metadata["schematic_instance_refdes_map"] = placed_instance_records

        inserted_supply_symbols: list[dict[str, str]] = []
        supply_supported = False

        connection_map = _build_net_connection_map(
            effective_nets=effective_nets,
            refdes_map=refdes_map,
            placed_refs=placed_refs,
            valid_pins_by_ref=valid_pins_by_ref,
            resolved_anchor_by_ref_pin=resolved_anchor_by_ref_pin,
            placement_map=placement_map,
        )
        project.metadata["schematic_connection_map_size"] = len(connection_map)

        # Stage 3: board-derived net connection map.
        board_net_map = build_board_derived_net_connection_map(connection_map)
        project.metadata["schematic_board_net_connection_map"] = board_net_map.as_report_dict()

        # Stage 5: net-attachment planning.
        net_plan = build_net_attachment_plan(
            connection_map=connection_map,
            placement_map=placement_map,
            all_anchor_points=all_anchor_points,
            resolved_anchor_by_ref_pin=resolved_anchor_by_ref_pin,
            should_draw_net=_should_draw_net,
            should_draw_net_with_stub_labels=_should_draw_net_with_stub_labels,
            build_stub_label_paths_for_net=_build_stub_label_paths_for_net,
            route_net_paths=_route_net_paths,
            legacy_chain_paths_for_net=_legacy_chain_paths_for_net,
            normalize_power_net_name=_normalize_power_net_name,
            append_occupied_segments=_append_occupied_segments,
            point_key=_point_key,
            label_spec_for_path=_label_spec_for_path,
            stub_length_mm=_SCHEMATIC_DEFAULT_GRID_MM,
            snap_to_default_grid=snap_to_default_grid,
            snap_path_to_grid=_snap_path_to_schematic_grid,
        )
        project.metadata["schematic_net_attachment_plan"] = net_plan.as_report_dict()

        # Stage 6: wire rendering from planned attachments.
        lines.extend(
            emit_net_attachment_lines(
                plans=net_plan.plans,
                use_inch_output=use_inch_output,
                quote_token=_quote_token,
                coord_for_output=_coord_for_schematic_output,
            )
        )
        occupied_segments = net_plan.occupied_segments
        pending_label_stubs = list(net_plan.pending_label_stubs)
        connected_component_refs = net_plan.connected_component_refs
        connected_pin_keys = net_plan.connected_pin_keys

        annotation_specs = _collect_schematic_annotation_specs(project.sheets)
        if annotation_specs:
            annotation_obstacles = _label_obstacle_points(
                placement_map=placement_map,
                anchor_map=anchor_map,
                component_radii_mm=component_radii,
            )
            spread_annotations = _spread_schematic_annotation_specs(
                annotation_specs=annotation_specs,
                occupied_points=annotation_obstacles,
            )
            for text, text_x_mm, text_y_mm in spread_annotations:
                if snap_to_default_grid:
                    text_x_mm, text_y_mm = _snap_point_to_schematic_grid((text_x_mm, text_y_mm))
                text_x, text_y = _point_for_schematic_output((text_x_mm, text_y_mm), use_inch_output)
                lines.append(f"TEXT '{text}' ({text_x:.4f} {text_y:.4f});")

        if pending_label_stubs:
            lines.append("CHANGE XREF ON;")
            label_size = _coord_for_schematic_output(1.27, use_inch_output)
            lines.append(f"CHANGE SIZE {label_size:.4f};")
            seen_points: set[tuple[float, float, float, float]] = set()
            adjusted_labels = _dedupe_label_specs(
                [
                    (
                        item.net_name,
                        item.pick_x_mm,
                        item.pick_y_mm,
                        item.label_x_mm,
                        item.label_y_mm,
                    )
                    for item in pending_label_stubs
                ]
            )
            emitted_labels: list[tuple[str, float, float, float, float]] = []
            for _net_name, pick_x, pick_y, label_x, label_y in adjusted_labels:
                if not _point_touches_any_segment((pick_x, pick_y), occupied_segments):
                    continue
                if snap_to_default_grid:
                    label_x, label_y = _snap_point_to_schematic_grid((label_x, label_y))
                pick_out_x, pick_out_y = _point_for_schematic_output((pick_x, pick_y), use_inch_output)
                label_out_x, label_out_y = _point_for_schematic_output((label_x, label_y), use_inch_output)
                key = (
                    round(pick_out_x, 4),
                    round(pick_out_y, 4),
                    round(label_out_x, 4),
                    round(label_out_y, 4),
                )
                if key in seen_points:
                    continue
                seen_points.add(key)
                # LABEL requires a pick point on a net segment and a placement point.
                lines.append(
                    f"LABEL ({pick_out_x:.4f} {pick_out_y:.4f}) ({label_out_x:.4f} {label_out_y:.4f});"
                )
                # Keep emitted label geometry in internal mm space for
                # organization/draw validation, while script output uses
                # converted units above.
                emitted_labels.append((_net_name, pick_x, pick_y, label_x, label_y))
        else:
            adjusted_labels = []
            emitted_labels = []

        lines = _orthogonalize_schematic_orientations(lines)
        project.metadata["schematic_organization_metrics"] = _schematic_organization_metrics(
            project=project,
            effective_nets=effective_nets,
            placement_map=placement_map,
            component_radii_mm=component_radii,
            occupied_segments=occupied_segments,
            labels=emitted_labels,
            connected_refs=connected_component_refs,
            layout_metadata=layout_metadata,
        )
        draw_metrics = _schematic_draw_metrics(
            placed_refs=placed_refs,
            connected_refs=connected_component_refs,
            connected_pin_keys=connected_pin_keys,
            resolved_anchor_by_ref_pin=resolved_anchor_by_ref_pin,
            occupied_segments=occupied_segments,
            emitted_labels=emitted_labels,
            unresolved_pin_anchor_count=unresolved_pin_anchor_count,
        )
        project.metadata["schematic_draw_metrics"] = draw_metrics
        project.metadata["schematic_unresolved_pin_anchors"] = unresolved_pin_anchor_items
        pipeline_validation = _schematic_pipeline_validation_summary(
            placeable_refs=placeable_refs,
            geometry_maps=geometry_maps,
            board_net_map=board_net_map,
            placement_stage=placement_stage,
            net_plan=net_plan,
            draw_metrics=draw_metrics,
            unresolved_pin_anchor_count=unresolved_pin_anchor_count,
            orientation_records=orientation_records_by_ref,
            placement_commands=placement_commands_by_ref,
        )
        project.metadata["schematic_pipeline_validation_summary"] = pipeline_validation
        if not bool(pipeline_validation.get("valid", True)):
            project.events.append(
                project_event(
                    Severity.WARNING,
                    "SCHEMATIC_PIPELINE_VALIDATION_FAILED",
                    "Schematic pipeline validation reported issues",
                    {"issues": list(pipeline_validation.get("issues", []))[:100]},
                )
            )
        project.metadata["schematic_pin_anchor_diagnostics"] = _schematic_pin_anchor_diagnostics(
            project=project,
            connection_map=connection_map,
            occupied_segments=occupied_segments,
            refdes_map=refdes_map,
        )
        if unresolved_pin_anchor_count > 0:
            project.events.append(
                project_event(
                    Severity.WARNING,
                    "SCHEMATIC_UNRESOLVED_PIN_ANCHORS",
                    f"Schematic draw used fallback anchors for {unresolved_pin_anchor_count} pin connections",
                    {
                        "count": unresolved_pin_anchor_count,
                        "examples": unresolved_pin_anchor_items[:50],
                    },
                )
            )
        project.metadata["supply_symbols_inserted"] = inserted_supply_symbols
        project.metadata["supply_symbols_mode"] = "disabled"
        return lines


def _build_refdes_map(project: Project) -> dict[str, str]:
    return _shared_build_refdes_map(project)


def _coalesced_nets(
    project: Project,
    net_alias: dict[str, str],
    refdes_map: dict[str, str],
    valid_pins_by_ref: dict[str, set[str]],
) -> list[Net]:
    if not project.nets:
        return []

    ordered_names: list[str] = []
    nodes_by_name: dict[str, list[tuple[str, str]]] = {}
    seen_by_name: dict[str, set[tuple[str, str]]] = {}
    candidate_nets_by_node: dict[tuple[str, str], set[str]] = defaultdict(set)
    fallback_idx = 1

    for net in project.nets:
        raw_name = str(getattr(net, "name", "") or "").strip()
        canonical = net_alias.get(raw_name, raw_name)
        if not canonical:
            canonical = f"N$AUTO{fallback_idx}"
            fallback_idx += 1

        if canonical not in nodes_by_name:
            ordered_names.append(canonical)
            nodes_by_name[canonical] = []
            seen_by_name[canonical] = set()

        for node in getattr(net, "nodes", []):
            raw_ref = str(getattr(node, "refdes", "") or "").strip()
            pin = str(getattr(node, "pin", "") or "").strip()
            if not raw_ref or not pin:
                continue
            ref = refdes_map.get(raw_ref, _sanitize_refdes(raw_ref))
            valid_pins = valid_pins_by_ref.get(ref, set())
            if valid_pins and pin not in valid_pins:
                # Drop stale/invalid pin aliases (for example inferred + source-
                # schematic mixed node IDs like e36) so they cannot perturb net
                # coalescing or layout routing.
                continue
            key = (ref, pin)
            if key in seen_by_name[canonical]:
                continue
            seen_by_name[canonical].add(key)
            nodes_by_name[canonical].append(key)
            candidate_nets_by_node[key].add(canonical)

    preferred_net_by_node: dict[tuple[str, str], str] = {}
    for node_key, net_names in candidate_nets_by_node.items():
        preferred_net_by_node[node_key] = _preferred_merged_net_name(net_names)

    remapped_nodes: dict[str, list[tuple[str, str]]] = {name: [] for name in ordered_names}
    remapped_seen: dict[str, set[tuple[str, str]]] = {name: set() for name in ordered_names}
    for name in ordered_names:
        for node_key in nodes_by_name[name]:
            target = preferred_net_by_node.get(node_key, name)
            if target not in remapped_nodes:
                remapped_nodes[target] = []
                remapped_seen[target] = set()
                ordered_names.append(target)
            if node_key in remapped_seen[target]:
                continue
            remapped_seen[target].add(node_key)
            remapped_nodes[target].append(node_key)

    out: list[Net] = []
    for name in ordered_names:
        merged_nodes = [NetNode(refdes=ref, pin=pin) for ref, pin in remapped_nodes.get(name, [])]
        if not merged_nodes:
            continue
        out.append(Net(name=name, nodes=merged_nodes))
    return out


def _preferred_merged_net_name(names: set[str]) -> str:
    cleaned = [str(name or "").strip() for name in names if str(name or "").strip()]
    if not cleaned:
        return "N$AUTO"

    def sort_key(value: str) -> tuple[int, int, int, str]:
        upper = value.upper()
        anonymous = 1 if upper.startswith("N$") else 0
        # Penalize synthetic pin-style names like R6_1 or U2_2.
        pin_like = 1 if re.match(r"^[A-Z]+[0-9]+_[A-Z0-9]+$", upper) else 0
        return (anonymous, pin_like, len(value), upper)

    return sorted(cleaned, key=sort_key)[0]


def _sanitize_refdes(value: str) -> str:
    return _shared_sanitize_refdes(value)


def _add_part_name_token(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return text
    # Quote all part names in ADD commands to avoid ambiguity with command tokens.
    return _quote_token(text)


def _component_refdes_key(component, ordinal: int) -> str:
    return _shared_component_instance_key(component, ordinal)


def _resolve_component_refdes(component, refdes_map: dict[str, str]) -> str:
    return _shared_resolve_component_refdes(component, refdes_map)


def _quote_token(value: str) -> str:
    text = str(value or "").replace("'", "")
    return f"'{text}'"


def _component_value_for_schematic(component) -> str:
    attrs = getattr(component, "attributes", {}) or {}
    package_hints = _value_package_hints(component)

    if _is_resistor_or_capacitor(component):
        rc_value = str(getattr(component, "value", "") or "").strip()
        if rc_value:
            return _sanitize_display_value(rc_value, package_hints)
        fallback = str(attrs.get("Value") or "").strip()
        if fallback:
            return _sanitize_display_value(fallback, package_hints)
        return ""

    mpn = _first_non_empty(
        getattr(component, "mpn", None),
        attrs.get("Manufacturer Part"),
        attrs.get("Manufacturer Part Number"),
        attrs.get("MPN"),
        attrs.get("Part Number"),
        attrs.get("part_number"),
    )
    if mpn:
        return _sanitize_display_value(mpn, package_hints)

    return _sanitize_display_value(str(getattr(component, "value", "") or "").strip(), package_hints)


def _is_resistor_or_capacitor(component) -> bool:
    refdes = str(getattr(component, "refdes", "") or "").strip().upper()
    if refdes.startswith(("R", "C")):
        return True
    attrs = getattr(component, "attributes", {}) or {}
    cls = str(attrs.get("component_class") or "").strip().lower()
    return cls in {"resistor", "capacitor"}


def _first_non_empty(*values) -> str:
    for raw in values:
        text = str(raw or "").strip()
        if text:
            return text
    return ""


def _value_package_hints(component) -> list[str]:
    attrs = getattr(component, "attributes", {}) or {}
    values = [
        getattr(component, "package_id", None),
        attrs.get("package_name"),
        attrs.get("package"),
        attrs.get("Package"),
        attrs.get("footprint"),
        attrs.get("Footprint"),
    ]
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        text = str(raw or "").strip()
        key = text.upper()
        if not text or key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _sanitize_display_value(value_text: str, package_hints: list[str]) -> str:
    text = str(value_text or "").strip()
    if not text:
        return ""

    removal_tokens: set[str] = set()
    for hint in package_hints:
        token_src = str(hint or "").upper()
        if not token_src:
            continue
        # Common package-size tokens we want hidden from VALUE display.
        removal_tokens.update(re.findall(r"(?<!\d)(?:0201|0402|0603|0805|1206|1210|1812|2010|2512)(?!\d)", token_src))
        removal_tokens.update(re.findall(r"\b\d+(?:\.\d+)?MM\b", token_src))
        removal_tokens.update(re.findall(r"P\d+(?:\.\d+)?", token_src))

    cleaned = text
    for token in sorted(removal_tokens, key=len, reverse=True):
        pattern = rf"(?i)(?:(?<=^)|(?<=[_\-/\s])){re.escape(token)}(?:(?=$)|(?=[_\-/\s]))"
        cleaned = re.sub(pattern, "", cleaned)

    # Clean connector/package separators after token removal.
    cleaned = re.sub(r"[ _\-/]{2,}", "-", cleaned)
    cleaned = re.sub(r"\(\s*\)", "", cleaned)
    cleaned = re.sub(r"[\s_\-/]+\)", ")", cleaned)
    cleaned = re.sub(r"\([\s_\-/]+", "(", cleaned)
    cleaned = re.sub(r"\s*\(\s*$", "", cleaned)
    cleaned = re.sub(r"^\s*\)\s*", "", cleaned)
    cleaned = re.sub(r"^[\s_\-/]+|[\s_\-/]+$", "", cleaned).strip()
    cleaned = re.sub(r"\s{2,}", " ", cleaned)

    return cleaned or text


def _snap_to_schematic_grid(value_mm: float) -> float:
    try:
        value = float(value_mm)
    except Exception:
        value = 0.0
    return round(round(value / _SCHEMATIC_DEFAULT_GRID_MM) * _SCHEMATIC_DEFAULT_GRID_MM, 4)


def _coord_for_schematic_output(value_mm: float, use_inch_output: bool) -> float:
    value = float(value_mm)
    if use_inch_output:
        return value / _MM_PER_INCH
    return value


def _point_for_schematic_output(
    point_mm: tuple[float, float],
    use_inch_output: bool,
) -> tuple[float, float]:
    return (
        _coord_for_schematic_output(point_mm[0], use_inch_output),
        _coord_for_schematic_output(point_mm[1], use_inch_output),
    )


def _snap_point_to_schematic_grid(point: tuple[float, float]) -> tuple[float, float]:
    return (_snap_to_schematic_grid(point[0]), _snap_to_schematic_grid(point[1]))


def _snap_path_to_schematic_grid(path: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not path:
        return path
    if len(path) <= 2:
        return _dedupe_consecutive_points(path)
    snapped: list[tuple[float, float]] = [path[0]]
    for x_mm, y_mm in path[1:-1]:
        snapped.append(_snap_point_to_schematic_grid((x_mm, y_mm)))
    snapped.append(path[-1])
    return _dedupe_consecutive_points(snapped)


def _snap_placement_map_to_grid(
    placement_map: dict[str, tuple[float, float]],
) -> dict[str, tuple[float, float]]:
    out: dict[str, tuple[float, float]] = {}
    for ref, point in placement_map.items():
        out[ref] = _snap_point_to_schematic_grid(point)
    return out


def _translate_placement_map_into_visible_window(
    placement_map: dict[str, tuple[float, float]],
    snap_to_default_grid: bool,
    min_margin_mm: float = 20.0,
) -> dict[str, tuple[float, float]]:
    if not placement_map:
        return placement_map

    xs = [point[0] for point in placement_map.values()]
    ys = [point[1] for point in placement_map.values()]
    min_x = min(xs)
    min_y = min(ys)

    raw_shift_x = max(0.0, float(min_margin_mm) - float(min_x))
    raw_shift_y = max(0.0, float(min_margin_mm) - float(min_y))

    if raw_shift_x <= 0.0 and raw_shift_y <= 0.0:
        return placement_map

    if snap_to_default_grid:
        shift_x = _grid_aligned_positive_offset(raw_shift_x)
        shift_y = _grid_aligned_positive_offset(raw_shift_y)
    else:
        shift_x = raw_shift_x
        shift_y = raw_shift_y

    out: dict[str, tuple[float, float]] = {}
    for ref, (x_mm, y_mm) in placement_map.items():
        tx = float(x_mm) + float(shift_x)
        ty = float(y_mm) + float(shift_y)
        if snap_to_default_grid:
            tx, ty = _snap_point_to_schematic_grid((tx, ty))
        out[ref] = (tx, ty)
    return out


def _grid_aligned_positive_offset(raw_offset_mm: float) -> float:
    if raw_offset_mm <= 0.0:
        return 0.0
    steps = int(math.ceil(float(raw_offset_mm) / _SCHEMATIC_DEFAULT_GRID_MM))
    return round(float(steps) * _SCHEMATIC_DEFAULT_GRID_MM, 4)


def _orthogonalize_schematic_orientations(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("ADD "):
            out.append(_snap_add_orientation_token(line))
            continue
        if stripped.startswith("ROTATE "):
            out.append(_snap_orientation_suffix(line))
            continue
        if stripped.startswith("TEXT "):
            out.append(_snap_orientation_suffix(line))
            continue
        out.append(line)
    return out


def _snap_add_orientation_token(line: str) -> str:
    pattern = re.compile(r"^(ADD\s+\S+\s+\S+\s+)(M?R)(-?\d+(?:\.\d+)?)(\s+\(.*)$")
    match = pattern.match(line)
    if match is None:
        return _snap_orientation_suffix(line)
    prefix, orientation_kind, angle_text, suffix = match.groups()
    try:
        angle = float(angle_text)
    except Exception:
        return line
    snapped = int(round(angle / 90.0)) * 90
    snapped %= 360
    return f"{prefix}{orientation_kind}{snapped}{suffix}"


def _snap_orientation_suffix(line: str) -> str:
    pattern = re.compile(r"(.*\s)(M?R)(-?\d+(?:\.\d+)?)(\s*;)\s*$")
    match = pattern.match(line)
    if not match:
        return line
    prefix, orientation_kind, angle_text, suffix = match.groups()
    try:
        angle = float(angle_text)
    except Exception:
        return line
    snapped = int(round(angle / 90.0)) * 90
    snapped %= 360
    return f"{prefix}{orientation_kind}{snapped}{suffix}"


def _script_device_token(device_id: str, library_paths: dict[str, str]) -> str:
    text = str(device_id or "").strip()
    if "@" in text:
        return text
    if ":" in text:
        lib, dev = text.split(":", 1)
        lib = lib.strip()
        dev = dev.strip()
        if dev:
            if lib:
                lib_ref = _resolve_library_ref(lib, library_paths)
                return f"{dev}@{lib_ref}"
            return dev
    return text


def _normalized_library_paths(values: dict[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key, value in values.items():
        text_key = str(key or "").strip()
        text_value = str(value or "").strip().replace("\\", "/")
        if not text_key or not text_value:
            continue
        normalized[text_key] = text_value
        normalized[_norm_library_key(text_key)] = text_value
    return normalized


def _resolve_library_ref(library_name: str, library_paths: dict[str, str]) -> str:
    if library_name in library_paths:
        return library_paths[library_name]
    normalized = _norm_library_key(library_name)
    if normalized in library_paths:
        return library_paths[normalized]
    return library_name


def _norm_library_key(value: str) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def _resolve_schematic_layout_mode(value: str | None) -> str:
    token = str(value or "").strip().lower()
    if token in {"board", "clustered", "hybrid", "human"}:
        return token
    return "board"


@dataclass(frozen=True)
class _HumanBlock:
    block_id: str
    kind: str
    refs: list[str]


def _auto_layout_positions(
    project: Project,
    refdes_map: dict[str, str],
    layout_mode: str,
    effective_nets: list[Net] | None = None,
) -> tuple[dict[str, tuple[float, float]], dict[str, Any]]:
    mode = _resolve_schematic_layout_mode(layout_mode)
    components = list(project.components)
    if not components:
        return {}, {"block_count": 0, "repeated_channel_groups": 0, "blocks": []}

    nets = list(effective_nets or project.nets)
    board_seed = _board_like_positions(components, refdes_map)
    grid_seed = _grid_layout_positions(components, refdes_map)

    if mode == "board":
        placement = board_seed if board_seed is not None else grid_seed
        return placement, _layout_metadata_for_mode(
            mode=mode,
            refs=list(placement.keys()),
        )

    if mode == "clustered":
        clustered = _clustered_layout_positions(project, refdes_map, seed_positions=None)
        if clustered:
            return clustered, _layout_metadata_for_mode(
                mode=mode,
                refs=list(clustered.keys()),
                block_count=len(_connectivity_clusters(project, refdes_map, {
                    _resolve_component_refdes(component, refdes_map): component
                    for component in project.components
                })),
            )
        if board_seed is not None:
            return board_seed, _layout_metadata_for_mode(mode=mode, refs=list(board_seed.keys()))
        return grid_seed, _layout_metadata_for_mode(mode=mode, refs=list(grid_seed.keys()))

    if mode == "human":
        human_positions, human_meta = _human_layout_positions(
            project=project,
            refdes_map=refdes_map,
            effective_nets=nets,
            board_seed=board_seed,
            grid_seed=grid_seed,
        )
        return human_positions, human_meta

    # Hybrid mode: preserve board-like neighborhood hints, then cluster-pack by
    # connectivity for readability, then compact RC passives near active blocks.
    seed_positions = board_seed if board_seed is not None else grid_seed
    # Keep small designs stable (existing behavior/fixtures) and apply clustering
    # when the part count is high enough to benefit from section grouping.
    if len(components) >= 6:
        clustered = _clustered_layout_positions(project, refdes_map, seed_positions=seed_positions)
        if clustered:
            seed_positions = clustered
    compacted = _compact_passive_rc_positions(project, refdes_map, seed_positions)
    return compacted, _layout_metadata_for_mode(mode=mode, refs=list(compacted.keys()))


def _layout_metadata_for_mode(
    mode: str,
    refs: list[str],
    block_count: int | None = None,
) -> dict[str, Any]:
    unique_refs = sorted(set(refs))
    return {
        "mode": mode,
        "block_count": int(block_count if block_count is not None else (1 if unique_refs else 0)),
        "repeated_channel_groups": 0,
        "blocks": [
            {
                "id": f"{mode}_0",
                "kind": mode,
                "component_count": len(unique_refs),
                "components": unique_refs,
            }
        ] if unique_refs else [],
    }


def _human_layout_positions(
    project: Project,
    refdes_map: dict[str, str],
    effective_nets: list[Net],
    board_seed: dict[str, tuple[float, float]] | None,
    grid_seed: dict[str, tuple[float, float]],
) -> tuple[dict[str, tuple[float, float]], dict[str, Any]]:
    component_by_ref = {
        _resolve_component_refdes(component, refdes_map): component
        for component in project.components
    }
    refs = sorted(component_by_ref.keys(), key=lambda ref: _component_ref_sort_key(ref, component_by_ref))
    if not refs:
        return {}, _layout_metadata_for_mode(mode="human", refs=[])

    seed_positions = board_seed if board_seed is not None else grid_seed
    adjacency, weighted_adjacency, nets_by_ref = _component_graph_from_nets(
        refs=refs,
        effective_nets=effective_nets,
        refdes_map=refdes_map,
    )
    blocks = _human_blocks(
        refs=refs,
        adjacency=adjacency,
        component_by_ref=component_by_ref,
        nets_by_ref=nets_by_ref,
        seed_positions=seed_positions,
    )
    blocks = _merge_support_blocks_into_active(
        blocks=blocks,
        component_by_ref=component_by_ref,
        nets_by_ref=nets_by_ref,
        seed_positions=seed_positions,
    )
    repeated_groups = _detect_repeated_channels(refs, component_by_ref)

    ordered_blocks = sorted(
        blocks,
        key=lambda block: _human_block_order_key(
            block=block,
            component_by_ref=component_by_ref,
            seed_positions=seed_positions,
        ),
    )

    lane_y = {
        "power": 20.0,
        "input": -55.0,
        "processing": -130.0,
        "output": -205.0,
        "support": -280.0,
    }
    lane_cursor: dict[str, float] = defaultdict(lambda: 20.0)
    lane_row_idx: dict[str, int] = defaultdict(int)
    row_max_width = 320.0
    block_gap_x = 22.0
    block_gap_y = 60.0

    placed: dict[str, tuple[float, float]] = {}
    block_metadata: list[dict[str, Any]] = []

    for block in ordered_blocks:
        local = _human_block_local_positions(
            block=block,
            component_by_ref=component_by_ref,
            weighted_adjacency=weighted_adjacency,
            seed_positions=seed_positions,
            nets_by_ref=nets_by_ref,
        )
        if not local:
            continue

        local_x = [point[0] for point in local.values()]
        local_y = [point[1] for point in local.values()]
        min_x = min(local_x)
        max_x = max(local_x)
        min_y = min(local_y)
        max_y = max(local_y)
        width = max(16.0, (max_x - min_x) + 20.0)

        lane = block.kind if block.kind in lane_y else "support"
        cursor = lane_cursor[lane]
        row = lane_row_idx[lane]
        if cursor > 20.0 and cursor + width > row_max_width:
            lane_row_idx[lane] = row + 1
            row = lane_row_idx[lane]
            cursor = 20.0
            lane_cursor[lane] = 20.0

        base_y = lane_y[lane] - (float(row) * block_gap_y)
        offset_x = cursor - min_x
        offset_y = base_y - max_y

        for ref, (x_mm, y_mm) in local.items():
            placed[ref] = (offset_x + x_mm, offset_y + y_mm)

        lane_cursor[lane] = cursor + width + block_gap_x
        block_metadata.append(
            {
                "id": block.block_id,
                "kind": lane,
                "component_count": len(block.refs),
                "components": sorted(block.refs),
            }
        )

    for ref in refs:
        if ref in placed:
            continue
        placed[ref] = seed_positions.get(ref, grid_seed.get(ref, (20.0, 20.0)))

    metadata = {
        "mode": "human",
        "block_count": len(block_metadata),
        "repeated_channel_groups": len(repeated_groups),
        "blocks": block_metadata,
    }
    return placed, metadata


def _component_graph_from_nets(
    refs: list[str],
    effective_nets: list[Net],
    refdes_map: dict[str, str],
) -> tuple[dict[str, set[str]], dict[str, dict[str, int]], dict[str, set[str]]]:
    refs_set = set(refs)
    adjacency: dict[str, set[str]] = {ref: set() for ref in refs}
    weighted: dict[str, dict[str, int]] = {ref: {} for ref in refs}
    nets_by_ref: dict[str, set[str]] = {ref: set() for ref in refs}

    for net in effective_nets:
        nodes = {
            refdes_map.get(node.refdes, _sanitize_refdes(node.refdes))
            for node in net.nodes
            if str(node.refdes or "").strip()
        }
        nodes = {node for node in nodes if node in refs_set}
        if not nodes:
            continue
        net_name = str(net.name or "").strip()
        for ref in nodes:
            if net_name:
                nets_by_ref[ref].add(net_name)
        if _net_is_global_for_blocking(net_name, len(nodes)):
            continue
        node_list = sorted(nodes)
        for idx, left in enumerate(node_list):
            for right in node_list[idx + 1 :]:
                adjacency[left].add(right)
                adjacency[right].add(left)
                weighted[left][right] = weighted[left].get(right, 0) + 1
                weighted[right][left] = weighted[right].get(left, 0) + 1
    return adjacency, weighted, nets_by_ref


def _net_is_global_for_blocking(name: str, fanout: int) -> bool:
    if fanout >= 12:
        return True
    return _normalize_power_net_name(name) is not None


def _human_blocks(
    refs: list[str],
    adjacency: dict[str, set[str]],
    component_by_ref: dict[str, object],
    nets_by_ref: dict[str, set[str]],
    seed_positions: dict[str, tuple[float, float]],
) -> list[_HumanBlock]:
    visited: set[str] = set()
    clusters: list[list[str]] = []
    for ref in refs:
        if ref in visited:
            continue
        queue = [ref]
        visited.add(ref)
        cluster: list[str] = []
        while queue:
            current = queue.pop(0)
            cluster.append(current)
            for nxt in sorted(adjacency.get(current, set())):
                if nxt in visited:
                    continue
                visited.add(nxt)
                queue.append(nxt)
        clusters.append(sorted(cluster, key=lambda item: _component_ref_sort_key(item, component_by_ref)))

    out: list[_HumanBlock] = []
    for idx, cluster in enumerate(clusters, start=1):
        kind = _human_block_kind(cluster, component_by_ref, nets_by_ref, seed_positions)
        out.append(_HumanBlock(block_id=f"B{idx:03d}", kind=kind, refs=cluster))
    return out


def _merge_support_blocks_into_active(
    blocks: list[_HumanBlock],
    component_by_ref: dict[str, object],
    nets_by_ref: dict[str, set[str]],
    seed_positions: dict[str, tuple[float, float]],
) -> list[_HumanBlock]:
    if len(blocks) < 2:
        return blocks

    active_blocks: list[_HumanBlock] = []
    support_blocks: list[_HumanBlock] = []
    for block in blocks:
        if _block_has_active_anchor(block, component_by_ref):
            active_blocks.append(block)
        else:
            support_blocks.append(block)

    if not active_blocks:
        return blocks

    merged_refs: dict[str, list[str]] = {block.block_id: list(block.refs) for block in active_blocks}
    passthrough_blocks: list[_HumanBlock] = []

    for block in support_blocks:
        if not _block_is_passive_support(block, component_by_ref):
            passthrough_blocks.append(block)
            continue
        target = _best_active_block_for_support(
            block=block,
            active_blocks=active_blocks,
            nets_by_ref=nets_by_ref,
            seed_positions=seed_positions,
        )
        if target is None:
            passthrough_blocks.append(block)
            continue
        merged_refs.setdefault(target.block_id, [])
        merged_refs[target.block_id].extend(block.refs)

    out: list[_HumanBlock] = []
    for block in blocks:
        if block.block_id in merged_refs:
            refs = sorted(set(merged_refs[block.block_id]))
            out.append(_HumanBlock(block_id=block.block_id, kind=block.kind, refs=refs))
        elif any(block.block_id == keep.block_id for keep in passthrough_blocks):
            out.append(block)
    return out


def _block_has_active_anchor(block: _HumanBlock, component_by_ref: dict[str, object]) -> bool:
    for ref in block.refs:
        if _is_primary_anchor(component_by_ref.get(ref)):
            return True
    return False


def _block_is_passive_support(block: _HumanBlock, component_by_ref: dict[str, object]) -> bool:
    passive_roles = {"resistor", "capacitor", "diode", "led", "inductor", "misc", "power"}
    if len(block.refs) > 8:
        return False
    for ref in block.refs:
        role = _component_role_token(component_by_ref.get(ref))
        if role not in passive_roles:
            return False
    return True


def _best_active_block_for_support(
    block: _HumanBlock,
    active_blocks: list[_HumanBlock],
    nets_by_ref: dict[str, set[str]],
    seed_positions: dict[str, tuple[float, float]],
) -> _HumanBlock | None:
    block_nets: set[str] = set()
    for ref in block.refs:
        block_nets.update(nets_by_ref.get(ref, set()))

    block_centroid = _refs_centroid(block.refs, seed_positions)

    ranked: list[tuple[int, float, str, _HumanBlock]] = []
    for candidate in active_blocks:
        candidate_nets: set[str] = set()
        for ref in candidate.refs:
            candidate_nets.update(nets_by_ref.get(ref, set()))
        shared_nets = len(block_nets.intersection(candidate_nets))
        candidate_centroid = _refs_centroid(candidate.refs, seed_positions)
        distance = math.hypot(block_centroid[0] - candidate_centroid[0], block_centroid[1] - candidate_centroid[1])
        ranked.append((-shared_nets, distance, candidate.block_id, candidate))

    ranked.sort(key=lambda item: (item[0], item[1], item[2]))
    if not ranked:
        return None
    best_shared = -ranked[0][0]
    best_distance = ranked[0][1]
    if best_shared <= 0 and best_distance > 90.0:
        return None
    return ranked[0][3]


def _refs_centroid(refs: Iterable[str], seed_positions: dict[str, tuple[float, float]]) -> tuple[float, float]:
    coords = [seed_positions.get(ref, (0.0, 0.0)) for ref in refs]
    if not coords:
        return (0.0, 0.0)
    return (
        sum(point[0] for point in coords) / float(len(coords)),
        sum(point[1] for point in coords) / float(len(coords)),
    )


def _human_block_kind(
    refs: list[str],
    component_by_ref: dict[str, object],
    nets_by_ref: dict[str, set[str]],
    seed_positions: dict[str, tuple[float, float]],
) -> str:
    if not refs:
        return "support"

    has_connector = False
    has_active = False
    has_output_hint = False
    has_input_hint = False
    has_power_hint = False
    for ref in refs:
        component = component_by_ref.get(ref)
        token = _component_role_token(component)
        has_connector = has_connector or token == "connector"
        has_active = has_active or token in {"ic", "transistor", "module", "regulator", "opamp"}
        if token in {"led", "relay"}:
            has_output_hint = True
        net_names = nets_by_ref.get(ref, set())
        for raw in net_names:
            norm = _norm_token(raw)
            if _normalize_power_net_name(raw):
                has_power_hint = True
            if norm.endswith("OUT") or norm.startswith("OUT") or "AUDIOOUT" in norm:
                has_output_hint = True
            if norm.endswith("IN") or norm.startswith("IN") or norm.startswith("VIN"):
                has_input_hint = True

    if has_power_hint and any(_component_role_token(component_by_ref.get(ref)) in {"regulator", "power"} for ref in refs):
        return "power"
    if has_connector:
        if has_output_hint and not has_input_hint:
            return "output"
        return "input"
    if has_power_hint and not has_active:
        return "power"
    if has_output_hint:
        return "output"
    if has_active:
        return "processing"

    # Fallback using board-seeded left/right distribution to keep flow sane.
    xs = [seed_positions.get(ref, (0.0, 0.0))[0] for ref in refs]
    if xs:
        centroid = sum(xs) / float(len(xs))
        if centroid < 80.0:
            return "input"
        if centroid > 180.0:
            return "output"
    return "support"


def _human_block_order_key(
    block: _HumanBlock,
    component_by_ref: dict[str, object],
    seed_positions: dict[str, tuple[float, float]],
) -> tuple[int, float, float, str]:
    lane_order = {
        "power": 0,
        "input": 1,
        "processing": 2,
        "output": 3,
        "support": 4,
    }
    coords = [seed_positions.get(ref, (0.0, 0.0)) for ref in block.refs]
    cx = sum(point[0] for point in coords) / float(len(coords)) if coords else 0.0
    cy = sum(point[1] for point in coords) / float(len(coords)) if coords else 0.0
    primary_ref = min(block.refs, key=lambda ref: _component_ref_sort_key(ref, component_by_ref))
    return (lane_order.get(block.kind, 99), cx, -cy, primary_ref)


def _human_block_local_positions(
    block: _HumanBlock,
    component_by_ref: dict[str, object],
    weighted_adjacency: dict[str, dict[str, int]],
    seed_positions: dict[str, tuple[float, float]],
    nets_by_ref: dict[str, set[str]],
) -> dict[str, tuple[float, float]]:
    refs = list(block.refs)
    if not refs:
        return {}

    anchors = [
        ref
        for ref in refs
        if _is_primary_anchor(component_by_ref.get(ref))
    ]
    if not anchors:
        anchors = [refs[0]]
    anchors = sorted(set(anchors), key=lambda ref: _component_ref_sort_key(ref, component_by_ref))

    anchor_positions: dict[str, tuple[float, float]] = {}
    cols = max(1, min(3, int(math.ceil(math.sqrt(len(anchors))))))
    anchor_pitch_x = 25.4
    anchor_pitch_y = 20.32
    for idx, anchor_ref in enumerate(anchors):
        row = idx // cols
        col = idx % cols
        anchor_positions[anchor_ref] = (
            col * anchor_pitch_x,
            -row * anchor_pitch_y,
        )

    local = dict(anchor_positions)
    assigned_anchor: dict[str, str] = {}
    slot_index: dict[str, int] = defaultdict(int)
    slot_offsets = [
        (-10.16, 6.35),
        (10.16, 6.35),
        (-10.16, -6.35),
        (10.16, -6.35),
        (0.0, 10.16),
        (0.0, -10.16),
        (-15.24, 0.0),
        (15.24, 0.0),
    ]

    for ref in refs:
        if ref in anchor_positions:
            continue
        best_anchor = _best_anchor_for_component(
            ref=ref,
            anchors=anchors,
            weighted_adjacency=weighted_adjacency,
            seed_positions=seed_positions,
        )
        if best_anchor is None:
            best_anchor = anchors[0]
        assigned_anchor[ref] = best_anchor
        slot = slot_index[best_anchor]
        slot_index[best_anchor] = slot + 1
        ring = slot // len(slot_offsets)
        dx, dy = slot_offsets[slot % len(slot_offsets)]
        scale = 1.0 + (0.45 * ring)
        ax, ay = anchor_positions[best_anchor]
        # Prefer power/ground support parts above/below anchors.
        net_names = nets_by_ref.get(ref, set())
        if any(_normalize_power_net_name(name) for name in net_names):
            if any(_norm_token(name) in {"GND", "AGND", "DGND", "PGND", "EARTH", "CHASSIS", "VSS"} for name in net_names):
                dx, dy = (dx, -abs(dy))
            else:
                dx, dy = (dx, abs(dy))
        local[ref] = (
            ax + (dx * scale),
            ay + (dy * scale),
        )

    _align_repeated_groups_local(
        refs=refs,
        local=local,
        component_by_ref=component_by_ref,
        assigned_anchor=assigned_anchor,
    )
    return local


def _best_anchor_for_component(
    ref: str,
    anchors: list[str],
    weighted_adjacency: dict[str, dict[str, int]],
    seed_positions: dict[str, tuple[float, float]],
) -> str | None:
    if not anchors:
        return None
    weights = weighted_adjacency.get(ref, {})

    def _key(anchor_ref: str) -> tuple[int, float, str]:
        weight = int(weights.get(anchor_ref, 0))
        sx, sy = seed_positions.get(ref, (0.0, 0.0))
        ax, ay = seed_positions.get(anchor_ref, (0.0, 0.0))
        distance = math.hypot(sx - ax, sy - ay)
        return (-weight, distance, anchor_ref)

    return sorted(anchors, key=_key)[0]


def _align_repeated_groups_local(
    refs: list[str],
    local: dict[str, tuple[float, float]],
    component_by_ref: dict[str, object],
    assigned_anchor: dict[str, str],
) -> None:
    groups: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for ref in refs:
        prefix, number = _split_refdes(ref)
        if number is None:
            continue
        component = component_by_ref.get(ref)
        role = _component_role_token(component)
        if role not in {"resistor", "capacitor", "diode", "led", "inductor"}:
            continue
        anchor = assigned_anchor.get(ref, "")
        groups[(prefix, role, anchor)].append(ref)

    for (_, _, anchor), group_refs in sorted(groups.items()):
        if len(group_refs) < 3:
            continue
        ordered = sorted(group_refs, key=lambda item: (_split_refdes(item)[1] or 0, item))
        pitch = 10.16
        if anchor and anchor in local:
            ax, ay = local[anchor]
            baseline_y = ay - 13.97
            start_x = ax - ((len(ordered) - 1) * pitch / 2.0)
        else:
            y_values = [local[item][1] for item in ordered if item in local]
            baseline_y = sum(y_values) / float(len(y_values)) if y_values else 0.0
            x_values = [local[item][0] for item in ordered if item in local]
            start_x = min(x_values) if x_values else 0.0
        for idx, ref in enumerate(ordered):
            local[ref] = (start_x + idx * pitch, baseline_y)


def _detect_repeated_channels(
    refs: list[str],
    component_by_ref: dict[str, object],
) -> list[list[str]]:
    groups: dict[tuple[str, str, str], list[tuple[int, str]]] = defaultdict(list)
    for ref in refs:
        prefix, number = _split_refdes(ref)
        if number is None:
            continue
        component = component_by_ref.get(ref)
        package = str(getattr(component, "package_id", "") or "")
        role = _component_role_token(component)
        groups[(prefix, role, package)].append((number, ref))

    out: list[list[str]] = []
    for key in sorted(groups.keys()):
        ordered = sorted(groups[key], key=lambda item: (item[0], item[1]))
        if len(ordered) < 2:
            continue
        current: list[str] = [ordered[0][1]]
        prev_num = ordered[0][0]
        for number, ref in ordered[1:]:
            if number - prev_num <= 2:
                current.append(ref)
            else:
                if len(current) >= 2:
                    out.append(current)
                current = [ref]
            prev_num = number
        if len(current) >= 2:
            out.append(current)
    return out


def _split_refdes(refdes: str) -> tuple[str, int | None]:
    token = _sanitize_refdes(refdes)
    match = re.match(r"^([A-Z_]+)(\d+)$", token.upper())
    if not match:
        return token.upper(), None
    return match.group(1), int(match.group(2))


def _is_primary_anchor(component: object) -> bool:
    role = _component_role_token(component)
    return role in {"ic", "module", "regulator", "connector", "transistor", "opamp", "power"}


def _component_role_token(component: object) -> str:
    if component is None:
        return "misc"

    attrs = getattr(component, "attributes", {}) or {}
    ref = _sanitize_refdes(str(getattr(component, "refdes", "") or "")).upper()
    source_name = str(getattr(component, "source_name", "") or "")
    blob = " ".join(
        str(item or "")
        for item in (
            source_name,
            attrs.get("Name"),
            attrs.get("Footprint"),
            attrs.get("Package"),
            attrs.get("component_class"),
            attrs.get("Device"),
            attrs.get("Designator"),
        )
    ).upper()

    if ref.startswith(("J", "CN", "HDR", "CON", "P")) or "CONNECTOR" in blob or "HEADER" in blob:
        return "connector"
    if ref.startswith("U") or "IC" in blob or "MCU" in blob or "CPU" in blob:
        if any(key in blob for key in ("REG", "LDO", "BUCK", "BOOST", "CONVERTER")):
            return "regulator"
        if any(key in blob for key in ("OPAMP", "OPA", "LMV", "TLV")):
            return "opamp"
        return "ic"
    if ref.startswith("Q") or "TRANSISTOR" in blob or "MOSFET" in blob or "BJT" in blob:
        return "transistor"
    if ref.startswith("R"):
        return "resistor"
    if ref.startswith("C"):
        return "capacitor"
    if ref.startswith("L"):
        return "inductor"
    if ref.startswith(("D", "LED")):
        if "LED" in blob or ref.startswith("LED"):
            return "led"
        return "diode"
    if ref.startswith(("K", "RL")) or "RELAY" in blob:
        return "relay"
    if ref.startswith("PWR"):
        return "power"
    if "MODULE" in blob:
        return "module"
    if str(attrs.get("component_class", "")).strip():
        return str(attrs.get("component_class", "")).strip().lower()
    return "misc"


def _grid_layout_positions(
    components,
    refdes_map: dict[str, str],
) -> dict[str, tuple[float, float]]:
    ordered = sorted(components, key=_placement_key)
    cols = max(1, min(12, int(len(ordered) ** 0.5) + 2))
    pitch_x = 25.0
    pitch_y = 15.0
    origin_x = 20.0
    origin_y = 20.0

    out: dict[str, tuple[float, float]] = {}
    for idx, component in enumerate(ordered):
        ref = _resolve_component_refdes(component, refdes_map)
        row = idx // cols
        col = idx % cols
        out[ref] = (
            origin_x + col * pitch_x,
            origin_y - row * pitch_y,
        )
    return out


def _clustered_layout_positions(
    project: Project,
    refdes_map: dict[str, str],
    seed_positions: dict[str, tuple[float, float]] | None,
) -> dict[str, tuple[float, float]]:
    if not project.components:
        return {}

    component_by_ref = {
        _resolve_component_refdes(component, refdes_map): component
        for component in project.components
    }
    clusters = _connectivity_clusters(project, refdes_map, component_by_ref)
    if not clusters:
        return {}

    cluster_order = sorted(
        clusters,
        key=lambda refs: _cluster_order_key(refs, component_by_ref, seed_positions),
    )
    return _pack_cluster_positions(cluster_order, component_by_ref, seed_positions)


def _connectivity_clusters(
    project: Project,
    refdes_map: dict[str, str],
    component_by_ref: dict[str, object],
) -> list[list[str]]:
    ordered_refs = [
        _resolve_component_refdes(component, refdes_map)
        for component in sorted(project.components, key=_placement_key)
    ]
    if not ordered_refs:
        return []

    adjacency: dict[str, set[str]] = {ref: set() for ref in ordered_refs}
    for net in project.nets:
        refs = sorted(
            {
                refdes_map.get(node.refdes, _sanitize_refdes(node.refdes))
                for node in net.nodes
                if str(node.refdes or "").strip()
            }
        )
        refs = [ref for ref in refs if ref in adjacency]
        if len(refs) < 2:
            continue
        for left in refs:
            adjacency[left].update(item for item in refs if item != left)

    visited: set[str] = set()
    out: list[list[str]] = []
    for ref in ordered_refs:
        if ref in visited:
            continue
        queue = [ref]
        visited.add(ref)
        cluster: list[str] = []
        while queue:
            current = queue.pop(0)
            cluster.append(current)
            for nxt in sorted(
                adjacency.get(current, set()),
                key=lambda item: _component_ref_sort_key(item, component_by_ref),
            ):
                if nxt in visited:
                    continue
                visited.add(nxt)
                queue.append(nxt)
        out.append(
            sorted(
                cluster,
                key=lambda item: _component_ref_sort_key(item, component_by_ref),
            )
        )
    return out


def _cluster_order_key(
    refs: list[str],
    component_by_ref: dict[str, object],
    seed_positions: dict[str, tuple[float, float]] | None,
) -> tuple[float, float, int, str]:
    coords = []
    if seed_positions is not None:
        coords = [seed_positions[ref] for ref in refs if ref in seed_positions]
    if coords:
        cx = sum(item[0] for item in coords) / float(len(coords))
        cy = sum(item[1] for item in coords) / float(len(coords))
        return (-round(cy, 4), round(cx, 4), -len(refs), refs[0])
    return (0.0, 0.0, -len(refs), refs[0])


def _component_ref_sort_key(ref: str, component_by_ref: dict[str, object]) -> tuple[int, str]:
    component = component_by_ref.get(ref)
    if component is None:
        return (99, str(ref or ""))
    return _placement_key(component)


def _pack_cluster_positions(
    cluster_order: list[list[str]],
    component_by_ref: dict[str, object],
    seed_positions: dict[str, tuple[float, float]] | None,
) -> dict[str, tuple[float, float]]:
    if not cluster_order:
        return {}

    out: dict[str, tuple[float, float]] = {}
    cursor_x = 20.0
    cursor_y = 20.0
    row_height = 0.0
    row_max_width = 260.0
    gap_x = 16.0
    gap_y = 14.0

    for refs in cluster_order:
        local = _cluster_local_positions(refs, component_by_ref, seed_positions)
        if not local:
            continue

        local_xs = [point[0] for point in local.values()]
        local_ys = [point[1] for point in local.values()]
        min_x = min(local_xs)
        max_x = max(local_xs)
        min_y = min(local_ys)
        max_y = max(local_ys)
        width = max(12.0, (max_x - min_x) + 12.0)
        height = max(10.0, (max_y - min_y) + 10.0)

        if cursor_x > 20.0 and cursor_x + width > row_max_width:
            cursor_x = 20.0
            cursor_y -= row_height + gap_y
            row_height = 0.0

        offset_x = cursor_x - min_x
        offset_y = cursor_y - max_y
        for ref, (lx, ly) in local.items():
            out[ref] = (offset_x + lx, offset_y + ly)

        cursor_x += width + gap_x
        row_height = max(row_height, height)

    return out


def _cluster_local_positions(
    refs: list[str],
    component_by_ref: dict[str, object],
    seed_positions: dict[str, tuple[float, float]] | None,
) -> dict[str, tuple[float, float]]:
    if not refs:
        return {}

    ordered_refs = sorted(refs, key=lambda item: _component_ref_sort_key(item, component_by_ref))
    if seed_positions is not None:
        seed_coords = [seed_positions[ref] for ref in ordered_refs if ref in seed_positions]
        if len(seed_coords) == len(ordered_refs):
            min_x = min(item[0] for item in seed_coords)
            max_x = max(item[0] for item in seed_coords)
            min_y = min(item[1] for item in seed_coords)
            max_y = max(item[1] for item in seed_coords)
            span_x = max_x - min_x
            span_y = max_y - min_y
            if span_x > 1e-6 or span_y > 1e-6:
                scale = 0.65
                return {
                    ref: (
                        (seed_positions[ref][0] - min_x) * scale,
                        (seed_positions[ref][1] - min_y) * scale,
                    )
                    for ref in ordered_refs
                }

    cols = max(1, min(5, int(len(ordered_refs) ** 0.5) + 1))
    pitch_x = 12.7
    pitch_y = 10.16
    local: dict[str, tuple[float, float]] = {}
    for idx, ref in enumerate(ordered_refs):
        row = idx // cols
        col = idx % cols
        local[ref] = (col * pitch_x, -row * pitch_y)
    return local


def _component_layout_radii_mm(
    project: Project,
    refdes_map: dict[str, str],
    library_paths: dict[str, str],
) -> dict[str, float]:
    symbol_lookup = {symbol.symbol_id: symbol for symbol in project.symbols}
    package_pin_count: dict[str, int] = {}
    for package in project.packages:
        count = len([pad for pad in package.pads if str(pad.pad_number or "").strip()])
        package_pin_count[package.package_id] = count
        package_pin_count[package.name] = count

    out: dict[str, float] = {}
    external_pin_cache: dict[tuple[str, str], dict[str, _CanonicalPinLocal]] = {}
    for component in project.components:
        ref = _resolve_component_refdes(component, refdes_map)
        pin_count = package_pin_count.get(str(component.package_id or "").strip(), 0)
        radius = _default_component_radius_mm(pin_count)

        symbol_id = str(component.symbol_id or "").strip()
        symbol = symbol_lookup.get(symbol_id) if symbol_id else None
        if symbol is not None:
            radius = max(radius, _symbol_radius_mm(symbol) + 1.27)

        external_offsets = _component_external_pin_offsets(
            component=component,
            library_paths=library_paths,
            cache=external_pin_cache,
        )
        if external_offsets:
            pin_radius = max(
                math.hypot(pin_local.x_mm, pin_local.y_mm)
                for pin_local in external_offsets.values()
            )
            radius = max(radius, pin_radius + 1.27)

        out[ref] = max(4.0, radius + 1.27)
    return out


def _default_component_radius_mm(pin_count: int) -> float:
    bounded = max(2, min(int(pin_count or 0), 32))
    return 3.81 + float(bounded) * 0.55


def _symbol_radius_mm(symbol) -> float:
    radius = 0.0
    for pin in getattr(symbol, "pins", []) or []:
        if getattr(pin, "at", None) is None:
            continue
        radius = max(radius, math.hypot(float(pin.at.x_mm), float(pin.at.y_mm)))

    for graphic in getattr(symbol, "graphics", []) or []:
        if not isinstance(graphic, dict):
            continue
        for x_mm, y_mm in _graphic_points_mm(graphic):
            radius = max(radius, math.hypot(float(x_mm), float(y_mm)))

    return max(radius, 4.0)


def _graphic_points_mm(graphic: dict[str, Any]) -> list[tuple[float, float]]:
    if str(graphic.get("kind", "")).strip().lower() == "origin":
        return []

    points: list[tuple[float, float]] = []
    x_keys = ("x_mm", "x1_mm", "x2_mm", "x3_mm", "x4_mm")
    y_keys = ("y_mm", "y1_mm", "y2_mm", "y3_mm", "y4_mm")
    for x_key in x_keys:
        for y_key in y_keys:
            if x_key not in graphic or y_key not in graphic:
                continue
            try:
                points.append((float(graphic[x_key]), float(graphic[y_key])))
            except Exception:
                continue

    raw_points = graphic.get("points")
    if isinstance(raw_points, list):
        for item in raw_points:
            if isinstance(item, dict):
                if "x_mm" in item and "y_mm" in item:
                    try:
                        points.append((float(item["x_mm"]), float(item["y_mm"])))
                    except Exception:
                        continue
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                try:
                    points.append((float(item[0]), float(item[1])))
                except Exception:
                    continue
    return points


def _component_external_pin_offsets(
    component,
    library_paths: dict[str, str],
    cache: dict[tuple[str, str], dict[str, _CanonicalPinLocal]],
) -> dict[str, _CanonicalPinLocal]:
    device_id = str(getattr(component, "device_id", "") or "").strip()
    if not device_id:
        return {}
    if ":" not in device_id:
        return {}

    lib_name, device_name = device_id.split(":", 1)
    lib_name = lib_name.strip()
    device_name = device_name.strip()
    if not lib_name or not device_name:
        return {}

    lib_ref = _resolve_library_ref(lib_name, library_paths)
    lib_path = Path(lib_ref.replace("\\", "/"))
    if not lib_path.exists():
        return {}

    cache_key = (str(lib_path), device_name)
    if cache_key not in cache:
        cache[cache_key] = _external_device_pin_offsets(lib_path, device_name)
    return cache.get(cache_key, {})


def _resolve_component_overlaps(
    project: Project,
    refdes_map: dict[str, str],
    placement_map: dict[str, tuple[float, float]],
    component_radii_mm: dict[str, float] | None = None,
) -> dict[str, tuple[float, float]]:
    if not placement_map:
        return placement_map

    ordered_refs = [
        _resolve_component_refdes(component, refdes_map)
        for component in sorted(project.components, key=_placement_key)
        if _resolve_component_refdes(component, refdes_map) in placement_map
    ]
    if not ordered_refs:
        ordered_refs = sorted(placement_map.keys())

    min_clearance_mm = 8.89
    resolved: dict[str, tuple[float, float]] = {}
    for ref in ordered_refs:
        proposed = placement_map.get(ref)
        if proposed is None:
            continue
        resolved[ref] = _nearest_clear_point(
            proposed,
            existing=resolved,
            occupied_ref=ref,
            min_clearance_mm=min_clearance_mm,
            component_radii_mm=component_radii_mm,
        )

    for ref, point in placement_map.items():
        if ref in resolved:
            continue
        resolved[ref] = _nearest_clear_point(
            point,
            existing=resolved,
            occupied_ref=ref,
            min_clearance_mm=min_clearance_mm,
            component_radii_mm=component_radii_mm,
        )
    return resolved


def _nearest_clear_point(
    proposed: tuple[float, float],
    existing: dict[str, tuple[float, float]],
    occupied_ref: str,
    min_clearance_mm: float,
    component_radii_mm: dict[str, float] | None = None,
) -> tuple[float, float]:
    px, py = proposed
    candidates = [(px, py)]
    base_radius = (
        float(component_radii_mm.get(occupied_ref, min_clearance_mm * 0.5))
        if component_radii_mm
        else min_clearance_mm * 0.5
    )
    step_mm = max(1.27, min_clearance_mm * 0.5, base_radius * 0.5)
    for ring in range(1, 26):
        offset = ring * step_mm
        candidates.extend(
            [
                (px + offset, py),
                (px - offset, py),
                (px, py + offset),
                (px, py - offset),
                (px + offset, py + offset),
                (px + offset, py - offset),
                (px - offset, py + offset),
                (px - offset, py - offset),
            ]
        )
    for candidate in candidates:
        if _point_has_clearance(
            candidate,
            existing,
            occupied_ref,
            min_clearance_mm,
            component_radii_mm=component_radii_mm,
        ):
            return candidate
    return proposed


def _placement_key(component) -> tuple[int, str]:
    ref = _sanitize_refdes(getattr(component, "refdes", ""))
    prefix = "".join(ch for ch in ref if ch.isalpha()).upper()
    order = {
        "PWR": 0,
        "CN": 1,
        "J": 1,
        "HDR": 1,
        "U": 2,
        "Q": 3,
        "D": 4,
        "LED": 4,
        "L": 5,
        "FB": 5,
        "R": 6,
        "C": 7,
        "TP": 8,
        "H": 9,
    }
    bucket = 99
    for key, value in order.items():
        if prefix.startswith(key):
            bucket = value
            break
    return (bucket, ref)


def _board_like_positions(project_components, refdes_map: dict[str, str]) -> dict[str, tuple[float, float]] | None:
    if not project_components:
        return None

    xs = [float(component.at.x_mm) for component in project_components]
    ys = [float(component.at.y_mm) for component in project_components]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    span_x = max_x - min_x
    span_y = max_y - min_y

    # Degenerate coordinate sets (all parts at same point) are likely schematic-only placeholders.
    # Fall back to a deterministic grid in that case.
    if span_x < 1e-6 and span_y < 1e-6:
        return None

    unique_xy = {(round(x, 4), round(y, 4)) for x, y in zip(xs, ys)}
    if len(unique_xy) < max(4, len(project_components) // 3):
        return None

    origin_x = 20.0
    origin_y = 20.0
    # Expand spacing compared with PCB while preserving layout likeness.
    scale = 1.8

    out: dict[str, tuple[float, float]] = {}
    occupancy: dict[tuple[float, float], int] = {}
    for component in project_components:
        ref = _resolve_component_refdes(component, refdes_map)
        x = origin_x + (float(component.at.x_mm) - min_x) * scale
        y = origin_y + (max_y - float(component.at.y_mm)) * scale
        key = (round(x, 3), round(y, 3))
        bump = occupancy.get(key, 0)
        occupancy[key] = bump + 1
        if bump:
            # Nudge exact duplicates to avoid stacking parts on top of each other.
            x += (bump % 5) * 1.5
            y += (bump // 5) * 1.5
        out[ref] = (x, y)
    return out


def _valid_pins_by_ref(project: Project) -> dict[str, set[str]]:
    return _shared_valid_pins_by_ref(project)


def _package_lookup(project: Project) -> dict[str, Any]:
    return _shared_package_lookup(project)


def _net_anchor(
    placement_map: dict[str, tuple[float, float]],
    anchor_map: dict[str, dict[str, _ResolvedPinAnchor]],
    refdes: str,
    pin: str,
) -> tuple[float, float]:
    pin_key = str(pin or "").strip()
    if refdes in anchor_map and pin_key in anchor_map[refdes]:
        anchor = anchor_map[refdes][pin_key]
        return anchor.x_mm, anchor.y_mm

    x, y = placement_map.get(refdes, (0.0, 0.0))
    if pin_key in {"1", "A"}:
        return x - 2.54, y
    if pin_key in {"2", "B"}:
        return x + 2.54, y

    if pin_key.isdigit():
        idx = int(pin_key)
    else:
        idx = sum(ord(ch) for ch in pin_key)
    dx = ((idx % 5) - 2) * 0.8
    dy = (((idx // 5) % 5) - 2) * 0.8
    return x + dx, y + dy


def _allow_simple_fallback_anchor(pin: str, valid_pins: set[str]) -> bool:
    pin_key = str(pin or "").strip().upper()
    if pin_key not in {"1", "2", "A", "B"}:
        return False
    if not valid_pins:
        return True
    if len(valid_pins) > 2:
        return False
    normalized = {item.upper() for item in valid_pins}
    return pin_key in normalized


def _should_draw_net(net_name: str, mapped_nodes: list[tuple[str, str, float, float]]) -> bool:
    del net_name
    if len(mapped_nodes) < 2:
        return False

    unique_nodes = {
        (str(ref).strip(), str(pin).strip())
        for ref, pin, _, _ in mapped_nodes
    }
    if len(unique_nodes) < 2:
        return False

    unique_points = {
        (round(float(x), 4), round(float(y), 4))
        for _, _, x, y in mapped_nodes
    }
    return len(unique_points) >= 2


def _compact_passive_rc_positions(
    project: Project,
    refdes_map: dict[str, str],
    placement_map: dict[str, tuple[float, float]],
) -> dict[str, tuple[float, float]]:
    if not placement_map:
        return placement_map

    component_by_ref = {
        _resolve_component_refdes(component, refdes_map): component
        for component in project.components
    }
    connected_refs_by_ref: dict[str, set[str]] = {}
    for net in project.nets:
        refs = {
            refdes_map.get(node.refdes, _sanitize_refdes(node.refdes))
            for node in net.nodes
            if str(node.refdes or "").strip()
        }
        refs = {ref for ref in refs if ref in component_by_ref}
        if len(refs) < 2:
            continue
        for ref in refs:
            connected_refs_by_ref.setdefault(ref, set()).update(other for other in refs if other != ref)

    out = dict(placement_map)
    for ref, component in component_by_ref.items():
        if not _component_is_rc_passive(component):
            continue
        neighbors = [
            other_ref
            for other_ref in sorted(connected_refs_by_ref.get(ref, set()))
            if not _component_is_rc_passive(component_by_ref.get(other_ref))
        ]
        if not neighbors:
            continue
        points = [
            out.get(other_ref, placement_map.get(other_ref, (0.0, 0.0)))
            for other_ref in neighbors
        ]
        target_x = sum(point[0] for point in points) / float(len(points))
        target_y = sum(point[1] for point in points) / float(len(points))
        seed = sum(ord(ch) for ch in ref)
        angle = (seed % 360) * math.pi / 180.0
        radius_mm = 3.2
        proposed = (
            target_x + math.cos(angle) * radius_mm,
            target_y + math.sin(angle) * radius_mm,
        )
        out[ref] = _nearest_free_point(proposed, out, occupied_ref=ref, min_clearance_mm=1.8)
    return out


def _nearest_free_point(
    proposed: tuple[float, float],
    placement_map: dict[str, tuple[float, float]],
    occupied_ref: str,
    min_clearance_mm: float,
) -> tuple[float, float]:
    px, py = proposed
    candidates = [(px, py)]
    for ring in range(1, 6):
        step = 0.8 * float(ring)
        candidates.extend(
            [
                (px + step, py),
                (px - step, py),
                (px, py + step),
                (px, py - step),
                (px + step, py + step),
                (px + step, py - step),
                (px - step, py + step),
                (px - step, py - step),
            ]
        )
    for candidate in candidates:
        if _point_has_clearance(candidate, placement_map, occupied_ref, min_clearance_mm):
            return candidate
    return proposed


def _point_has_clearance(
    point: tuple[float, float],
    placement_map: dict[str, tuple[float, float]],
    occupied_ref: str,
    min_clearance_mm: float,
    component_radii_mm: dict[str, float] | None = None,
) -> bool:
    occupied_radius = (
        float(component_radii_mm.get(occupied_ref, min_clearance_mm * 0.5))
        if component_radii_mm
        else min_clearance_mm * 0.5
    )
    for ref, other in placement_map.items():
        if ref == occupied_ref:
            continue
        other_radius = (
            float(component_radii_mm.get(ref, min_clearance_mm * 0.5))
            if component_radii_mm
            else min_clearance_mm * 0.5
        )
        required_clearance = max(min_clearance_mm, occupied_radius + other_radius)
        if math.hypot(point[0] - other[0], point[1] - other[1]) < required_clearance:
            return False
    return True


def _component_is_rc_passive(component) -> bool:
    if component is None:
        return False
    ref = _sanitize_refdes(getattr(component, "refdes", "")).upper()
    return ref.startswith("R") or ref.startswith("C")


def _label_spec_for_path(path: list[tuple[float, float]]) -> tuple[float, float, float, float] | None:
    segments = _path_segments(path)
    if not segments:
        return None

    # Prefer labeling at the terminal end of the routed net line so labels
    # stay visually associated with the segment endpoint.
    start, end = segments[-1]
    dx = end[0] - start[0]
    dy = end[1] - start[1]

    if abs(dx) >= abs(dy):
        direction = 1.0 if dx >= 0 else -1.0
        pick_x = end[0] - (0.2 * direction)
        pick_y = end[1]
        label_x = end[0]
        label_y = end[1]
    else:
        direction = 1.0 if dy >= 0 else -1.0
        pick_x = end[0]
        pick_y = end[1] - (0.2 * direction)
        label_x = end[0]
        label_y = end[1]

    return (pick_x, pick_y, label_x, label_y)


def _collect_schematic_annotation_specs(
    sheets: Iterable[Any],
) -> list[tuple[str, float, float, float]]:
    specs: list[tuple[str, float, float, float]] = []
    for sheet in sheets:
        annotations = getattr(sheet, "annotations", None)
        if not isinstance(annotations, list):
            continue
        for note in annotations:
            raw_text = str(getattr(note, "text", "") or "")
            text = raw_text.replace("'", "")
            if not text.strip():
                continue
            at = getattr(note, "at", None)
            try:
                base_x = float(getattr(at, "x_mm", 0.0) if at is not None else 0.0)
                base_y = float(getattr(at, "y_mm", 0.0) if at is not None else 0.0)
            except Exception:
                base_x = 0.0
                base_y = 0.0
            try:
                size_mm = float(getattr(note, "size_mm", _SCHEMATIC_DEFAULT_GRID_MM) or _SCHEMATIC_DEFAULT_GRID_MM)
            except Exception:
                size_mm = _SCHEMATIC_DEFAULT_GRID_MM
            line_pitch_mm = max(_SCHEMATIC_DEFAULT_GRID_MM, size_mm * 1.25)
            raw_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            if not raw_lines:
                raw_lines = [text]
            line_index = 0
            for raw_line in raw_lines:
                line_text = raw_line.strip()
                line_y = base_y - (line_index * line_pitch_mm)
                line_index += 1
                if not line_text:
                    continue
                specs.append((line_text, base_x, line_y, line_pitch_mm))
    return specs


def _spread_schematic_annotation_specs(
    annotation_specs: list[tuple[str, float, float, float]],
    occupied_points: list[tuple[float, float]] | None = None,
) -> list[tuple[str, float, float]]:
    if not annotation_specs:
        return []

    out: list[tuple[str, float, float]] = []
    used_points: list[tuple[float, float]] = []
    static_obstacles = list(occupied_points or [])
    offsets = _label_candidate_offsets(step_mm=_SCHEMATIC_DEFAULT_GRID_MM, rings=12)
    for text, at_x, at_y, line_pitch_mm in annotation_specs:
        chosen = (at_x, at_y)
        min_clearance_mm = max(_SCHEMATIC_DEFAULT_GRID_MM, float(line_pitch_mm) * 0.9)
        obstacle_clearance_mm = max(_SCHEMATIC_DEFAULT_GRID_MM, min_clearance_mm)
        fallback_choice = chosen
        fallback_score = _label_candidate_score(chosen, used_points, static_obstacles)
        for dx, dy in offsets:
            candidate = (at_x + dx, at_y + dy)
            candidate_score = _label_candidate_score(candidate, used_points, static_obstacles)
            if candidate_score > fallback_score:
                fallback_choice = candidate
                fallback_score = candidate_score
            if not _label_point_has_clearance(candidate, used_points, min_clearance_mm):
                continue
            if static_obstacles and not _label_point_has_clearance(
                candidate,
                static_obstacles,
                obstacle_clearance_mm,
            ):
                continue
            chosen = candidate
            break
        else:
            chosen = fallback_choice
        used_points.append(chosen)
        out.append((text, chosen[0], chosen[1]))
    return out


def _spread_label_specs(
    label_specs: list[tuple[str, float, float, float, float]],
    occupied_points: list[tuple[float, float]] | None = None,
) -> list[tuple[str, float, float, float, float]]:
    if not label_specs:
        return []

    out: list[tuple[str, float, float, float, float]] = []
    used_labels: list[tuple[float, float]] = []
    static_obstacles = list(occupied_points or [])
    min_clearance_mm = 1.905
    obstacle_clearance_mm = 3.175
    offsets = _label_candidate_offsets(step_mm=1.27, rings=14)
    for net_name, pick_x, pick_y, label_x, label_y in label_specs:
        chosen = (label_x, label_y)
        fallback_choice = chosen
        fallback_score = _label_candidate_score(chosen, used_labels, static_obstacles)
        for dx, dy in offsets:
            candidate = (label_x + dx, label_y + dy)
            candidate_score = _label_candidate_score(candidate, used_labels, static_obstacles)
            if candidate_score > fallback_score:
                fallback_choice = candidate
                fallback_score = candidate_score
            if not _label_point_has_clearance(candidate, used_labels, min_clearance_mm):
                continue
            if not _label_point_has_clearance(candidate, static_obstacles, obstacle_clearance_mm):
                continue
            chosen = candidate
            break
        else:
            chosen = fallback_choice
        used_labels.append(chosen)
        out.append((net_name, pick_x, pick_y, chosen[0], chosen[1]))
    return out


def _dedupe_label_specs(
    label_specs: list[tuple[str, float, float, float, float]],
) -> list[tuple[str, float, float, float, float]]:
    if not label_specs:
        return []

    out: list[tuple[str, float, float, float, float]] = []
    seen: set[tuple[str, float, float]] = set()
    for net_name, pick_x, pick_y, label_x, label_y in label_specs:
        key = (_norm_token(net_name), round(pick_x, 4), round(pick_y, 4))
        if key in seen:
            continue
        seen.add(key)
        out.append((net_name, pick_x, pick_y, label_x, label_y))
    return out


def _label_obstacle_points(
    placement_map: dict[str, tuple[float, float]],
    anchor_map: dict[str, dict[str, _ResolvedPinAnchor]],
    component_radii_mm: dict[str, float] | None = None,
) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    seen: set[tuple[float, float]] = set()
    for ref, (x, y) in placement_map.items():
        key = (round(float(x), 4), round(float(y), 4))
        if key in seen:
            continue
        seen.add(key)
        points.append((float(x), float(y)))
        if component_radii_mm:
            radius = max(1.27, float(component_radii_mm.get(ref, 4.0)))
            for scale in (0.65, 1.0):
                r = radius * scale
                for ox, oy in (
                    (r, 0.0),
                    (-r, 0.0),
                    (0.0, r),
                    (0.0, -r),
                    (r, r),
                    (r, -r),
                    (-r, r),
                    (-r, -r),
                ):
                    px = float(x) + ox
                    py = float(y) + oy
                    pkey = (round(px, 4), round(py, 4))
                    if pkey in seen:
                        continue
                    seen.add(pkey)
                    points.append((px, py))
    for pin_map in anchor_map.values():
        for anchor in pin_map.values():
            x, y = anchor.x_mm, anchor.y_mm
            key = (round(float(x), 4), round(float(y), 4))
            if key in seen:
                continue
            seen.add(key)
            points.append((float(x), float(y)))
    return points


def _label_candidate_offsets(step_mm: float, rings: int) -> list[tuple[float, float]]:
    offsets: list[tuple[float, float]] = [(0.0, 0.0)]
    max_ring = max(1, int(rings))
    for ring in range(1, max_ring + 1):
        delta = float(ring) * float(step_mm)
        offsets.extend(
            [
                (delta, 0.0),
                (-delta, 0.0),
                (0.0, delta),
                (0.0, -delta),
                (delta, delta),
                (delta, -delta),
                (-delta, delta),
                (-delta, -delta),
            ]
        )
    return offsets


def _label_point_has_clearance(
    point: tuple[float, float],
    used_points: list[tuple[float, float]],
    min_clearance_mm: float,
) -> bool:
    for other in used_points:
        if math.hypot(point[0] - other[0], point[1] - other[1]) < min_clearance_mm:
            return False
    return True


def _label_candidate_score(
    point: tuple[float, float],
    used_points: list[tuple[float, float]],
    obstacle_points: list[tuple[float, float]],
) -> float:
    min_label = _min_distance_to_points(point, used_points)
    min_obstacle = _min_distance_to_points(point, obstacle_points)
    return min_label + min_obstacle


def _min_distance_to_points(point: tuple[float, float], points: list[tuple[float, float]]) -> float:
    if not points:
        return 1_000_000.0
    px, py = point
    distances = [math.hypot(px - ox, py - oy) for ox, oy in points]
    return min(distances) if distances else 1_000_000.0


def _schematic_organization_metrics(
    project: Project,
    effective_nets: list[Net],
    placement_map: dict[str, tuple[float, float]],
    component_radii_mm: dict[str, float],
    occupied_segments: list[tuple[str, tuple[float, float], tuple[float, float]]],
    labels: list[tuple[str, float, float, float, float]],
    connected_refs: set[str],
    layout_metadata: dict[str, Any],
) -> dict[str, Any]:
    power_nets = sorted(
        {
            str(net.name or "").strip()
            for net in effective_nets
            if _normalize_power_net_name(net.name)
        }
    )
    ground_nets = sorted(
        {
            str(net.name or "").strip()
            for net in effective_nets
            if _is_ground_net_name(net.name)
        }
    )

    placed_refs = set(placement_map.keys())
    disconnected_refs = sorted(placed_refs - set(connected_refs))
    overlap_count = _placement_overlap_count(placement_map, component_radii_mm)
    orphan_label_count = _orphan_label_count(labels, occupied_segments)
    crossing_risk = _crossing_risk_score(occupied_segments)

    return {
        "layout_mode": str(layout_metadata.get("mode") or project.metadata.get("schematic_layout_mode") or "board"),
        "component_count": len(project.components),
        "block_count": int(layout_metadata.get("block_count") or 0),
        "block_kinds": [
            str(item.get("kind") or "")
            for item in layout_metadata.get("blocks", [])
            if isinstance(item, dict) and str(item.get("kind") or "")
        ],
        "repeated_channel_groups": int(layout_metadata.get("repeated_channel_groups") or 0),
        "recognized_power_nets": power_nets,
        "recognized_ground_nets": ground_nets,
        "overlap_count": int(overlap_count),
        "crossing_risk_score": int(crossing_risk),
        "disconnected_component_count": len(disconnected_refs),
        "disconnected_components": disconnected_refs[:200],
        "orphan_label_count": int(orphan_label_count),
        "label_count": len(labels),
    }


def _placement_overlap_count(
    placement_map: dict[str, tuple[float, float]],
    component_radii_mm: dict[str, float],
) -> int:
    refs = sorted(placement_map.keys())
    overlaps = 0
    for idx, left_ref in enumerate(refs):
        lx, ly = placement_map[left_ref]
        left_radius = max(2.54, float(component_radii_mm.get(left_ref, 4.0)) * 0.65)
        for right_ref in refs[idx + 1 :]:
            rx, ry = placement_map[right_ref]
            right_radius = max(2.54, float(component_radii_mm.get(right_ref, 4.0)) * 0.65)
            if math.hypot(lx - rx, ly - ry) < (left_radius + right_radius):
                overlaps += 1
    return overlaps


def _crossing_risk_score(
    occupied_segments: list[tuple[str, tuple[float, float], tuple[float, float]]],
) -> int:
    risk = 0
    for idx in range(len(occupied_segments)):
        left_net, left_start, left_end = occupied_segments[idx]
        for jdx in range(idx + 1, len(occupied_segments)):
            right_net, right_start, right_end = occupied_segments[jdx]
            if left_net == right_net:
                continue
            if _segments_share_endpoint(left_start, left_end, right_start, right_end):
                continue
            if _axis_segments_touch(left_start, left_end, right_start, right_end):
                risk += 1
    return risk


def _orphan_label_count(
    labels: list[tuple[str, float, float, float, float]],
    occupied_segments: list[tuple[str, tuple[float, float], tuple[float, float]]],
) -> int:
    if not labels:
        return 0
    orphans = 0
    for _, pick_x, pick_y, _, _ in labels:
        if not _point_touches_any_segment((pick_x, pick_y), occupied_segments):
            orphans += 1
    return orphans


def _schematic_draw_metrics(
    placed_refs: set[str],
    connected_refs: set[str],
    connected_pin_keys: set[tuple[str, str]],
    resolved_anchor_by_ref_pin: dict[tuple[str, str], _ResolvedPinAnchor],
    occupied_segments: list[tuple[str, tuple[float, float], tuple[float, float]]],
    emitted_labels: list[tuple[str, float, float, float, float]],
    unresolved_pin_anchor_count: int,
) -> dict[str, Any]:
    anchor_points = {
        _point_key((anchor.x_mm, anchor.y_mm))
        for key, anchor in resolved_anchor_by_ref_pin.items()
        if key in connected_pin_keys
    }
    label_pick_points = {
        _point_key((pick_x, pick_y))
        for _, pick_x, pick_y, _, _ in emitted_labels
    }

    degree: dict[tuple[float, float], int] = defaultdict(int)
    for _, start, end in occupied_segments:
        degree[_point_key(start)] += 1
        degree[_point_key(end)] += 1

    orphan_wire_endpoints = 0
    for point, count in degree.items():
        if count != 1:
            continue
        if point in anchor_points:
            continue
        if point in label_pick_points:
            continue
        orphan_wire_endpoints += 1

    label_only_connection_count = 0
    for _, pick_x, pick_y, _, _ in emitted_labels:
        if not _point_touches_any_segment((pick_x, pick_y), occupied_segments):
            label_only_connection_count += 1

    connected_pin_count = 0
    for key in connected_pin_keys:
        anchor = resolved_anchor_by_ref_pin.get(key)
        if anchor is None:
            continue
        if _point_key((anchor.x_mm, anchor.y_mm)) in degree:
            connected_pin_count += 1

    disconnected_component_count = len(set(placed_refs) - set(connected_refs))
    junction_count = sum(1 for count in degree.values() if count >= 3)

    return {
        "symbol_count": len(placed_refs),
        "connected_pin_count": connected_pin_count,
        "wire_segment_count": len(occupied_segments),
        "junction_count": junction_count,
        "orphan_wire_endpoint_count": orphan_wire_endpoints,
        "unresolved_pin_anchor_count": int(unresolved_pin_anchor_count),
        "label_only_connection_count": int(label_only_connection_count),
        "disconnected_component_count": disconnected_component_count,
    }


def _schematic_pipeline_validation_summary(
    placeable_refs: set[str],
    geometry_maps: Any,
    board_net_map: Any,
    placement_stage: Any,
    net_plan: Any,
    draw_metrics: dict[str, int | float],
    unresolved_pin_anchor_count: int,
    orientation_records: dict[str, dict[str, Any]],
    placement_commands: dict[str, str],
) -> dict[str, Any]:
    issues: list[str] = []

    placed_symbol_count = len(getattr(geometry_maps, "symbol_origins", {}) or {})
    if placed_symbol_count < len(placeable_refs):
        issues.append(
            f"missing_symbol_origins:{len(placeable_refs) - placed_symbol_count}"
        )

    placed_anchor_count = len(getattr(geometry_maps, "placed_pin_anchors", {}) or {})
    planned_endpoint_count = sum(
        len(getattr(plan, "endpoints", ()) or ())
        for plan in (getattr(net_plan, "plans", ()) or ())
    )
    if planned_endpoint_count > 0 and placed_anchor_count == 0:
        issues.append("no_placed_pin_anchors")

    board_net_count = len(getattr(board_net_map, "nets", ()) or ())
    planned_net_count = len(getattr(net_plan, "plans", ()) or ())
    if board_net_count > 0 and planned_net_count == 0:
        issues.append("no_net_attachment_plans")

    placement_entries = len(getattr(placement_stage, "entries", ()) or ())
    if placement_entries < len(placeable_refs):
        issues.append(f"missing_placement_entries:{len(placeable_refs) - placement_entries}")

    orientation_entry_count = len(orientation_records)
    if orientation_entry_count < len(placeable_refs):
        issues.append(f"missing_orientation_records:{len(placeable_refs) - orientation_entry_count}")

    placement_command_count = len(placement_commands)
    if placement_command_count < len(placeable_refs):
        issues.append(f"missing_placement_commands:{len(placeable_refs) - placement_command_count}")

    rotated_refs = {
        ref
        for ref, record in orientation_records.items()
        if int(record.get("rotation_deg", 0) or 0) % 360 != 0
    }
    rotated_refs_missing_add = {
        ref
        for ref in rotated_refs
        if " R0 " in str(placement_commands.get(ref, "") or "")
    }
    if rotated_refs_missing_add:
        issues.append(
            f"rotated_instances_emitted_as_default:{len(rotated_refs_missing_add)}"
        )

    orphan_endpoints = int(draw_metrics.get("orphan_wire_endpoint_count", 0) or 0)
    if orphan_endpoints > 0:
        issues.append(f"orphan_wire_endpoints:{orphan_endpoints}")

    label_only = int(draw_metrics.get("label_only_connection_count", 0) or 0)
    if label_only > 0:
        issues.append(f"label_only_connections:{label_only}")

    disconnected = int(draw_metrics.get("disconnected_component_count", 0) or 0)
    if disconnected > 0:
        issues.append(f"disconnected_components:{disconnected}")

    if int(unresolved_pin_anchor_count or 0) > 0:
        issues.append(f"unresolved_pin_anchors:{int(unresolved_pin_anchor_count)}")

    return {
        "valid": not issues,
        "issue_count": len(issues),
        "issues": issues,
        "checks": {
            "all_placeable_symbols_have_origins": placed_symbol_count >= len(placeable_refs),
            "all_connected_pins_have_anchor_map_entries": int(unresolved_pin_anchor_count or 0) == 0,
            "board_nets_have_attachment_plans": board_net_count == 0 or planned_net_count > 0,
            "all_placeable_components_have_placement_entries": placement_entries >= len(placeable_refs),
            "all_placeable_components_have_orientation_records": orientation_entry_count >= len(placeable_refs),
            "all_placeable_components_have_emitted_add_commands": placement_command_count >= len(placeable_refs),
            "rotated_instances_not_emitted_as_r0": len(rotated_refs_missing_add) == 0,
            "no_orphan_wire_endpoints": orphan_endpoints == 0,
            "no_orphan_label_only_connections": label_only == 0,
        },
        "counts": {
            "placeable_component_count": len(placeable_refs),
            "placed_symbol_count": placed_symbol_count,
            "placed_pin_anchor_count": placed_anchor_count,
            "board_net_count": board_net_count,
            "net_attachment_plan_count": planned_net_count,
            "planned_endpoint_count": planned_endpoint_count,
            "placement_entry_count": placement_entries,
            "orientation_record_count": orientation_entry_count,
            "placement_command_count": placement_command_count,
            "rotated_instance_count": len(rotated_refs),
            "rotated_instances_emitted_as_default_count": len(rotated_refs_missing_add),
            "orphan_wire_endpoint_count": orphan_endpoints,
            "label_only_connection_count": label_only,
            "disconnected_component_count": disconnected,
            "unresolved_pin_anchor_count": int(unresolved_pin_anchor_count or 0),
        },
    }


def _point_touches_any_segment(
    point: tuple[float, float],
    occupied_segments: list[tuple[str, tuple[float, float], tuple[float, float]]],
    eps: float = 1e-6,
) -> bool:
    px, py = point
    for _, start, end in occupied_segments:
        sx, sy = start
        ex, ey = end
        if math.isclose(sx, ex, abs_tol=eps):
            if math.isclose(px, sx, abs_tol=eps):
                y0, y1 = sorted((sy, ey))
                if y0 - eps <= py <= y1 + eps:
                    return True
            continue
        if math.isclose(sy, ey, abs_tol=eps):
            if math.isclose(py, sy, abs_tol=eps):
                x0, x1 = sorted((sx, ex))
                if x0 - eps <= px <= x1 + eps:
                    return True
    return False


def _schematic_pin_anchor_diagnostics(
    project: Project,
    connection_map: list[_NetConnection],
    occupied_segments: list[tuple[str, tuple[float, float], tuple[float, float]]],
    refdes_map: dict[str, str],
) -> list[dict[str, Any]]:
    component_by_ref = {
        _resolve_component_refdes(component, refdes_map): component
        for component in project.components
    }
    endpoints: list[tuple[float, float]] = []
    endpoints_by_key: dict[tuple[float, float], list[tuple[float, float]]] = defaultdict(list)
    for _, start, end in occupied_segments:
        for point in (start, end):
            key = _point_key(point)
            endpoints_by_key[key].append(point)
            endpoints.append(point)

    placement_commands = (
        project.metadata.get("schematic_instance_placement_commands", {})
        if isinstance(project.metadata, dict)
        else {}
    )
    if not isinstance(placement_commands, dict):
        placement_commands = {}
    orientation_records = (
        project.metadata.get("schematic_instance_orientation_records", {})
        if isinstance(project.metadata, dict)
        else {}
    )
    if not isinstance(orientation_records, dict):
        orientation_records = {}
    symbol_origin_map = (
        project.metadata.get("schematic_symbol_origin_map", {})
        if isinstance(project.metadata, dict)
        else {}
    )
    if not isinstance(symbol_origin_map, dict):
        symbol_origin_map = {}
    placed_symbols = {
        str(item.get("refdes") or ""): item
        for item in list(symbol_origin_map.get("placed_symbols", []) or [])
        if isinstance(item, dict) and str(item.get("refdes") or "")
    }
    symbol_geometry_map = (
        project.metadata.get("schematic_symbol_geometry_map", {})
        if isinstance(project.metadata, dict)
        else {}
    )
    if not isinstance(symbol_geometry_map, dict):
        symbol_geometry_map = {}
    symbol_defs = {
        str(item.get("symbol_id") or ""): item
        for item in list(symbol_geometry_map.get("symbols", []) or [])
        if isinstance(item, dict) and str(item.get("symbol_id") or "")
    }

    out: list[dict[str, Any]] = []
    for connection in connection_map:
        for node in connection.nodes:
            anchor = node.anchor
            anchor_point = (float(anchor.x_mm), float(anchor.y_mm))
            anchor_key = _point_key(anchor_point)
            exact = endpoints_by_key.get(anchor_key, [])
            nearest: tuple[float, float] | None = None
            delta_mm = 0.0
            if exact:
                nearest = exact[0]
            elif endpoints:
                nearest = min(
                    endpoints,
                    key=lambda point: math.hypot(point[0] - anchor_point[0], point[1] - anchor_point[1]),
                )
                delta_mm = float(math.hypot(nearest[0] - anchor_point[0], nearest[1] - anchor_point[1]))

            component = component_by_ref.get(node.refdes)
            device_id = str(getattr(component, "device_id", "") or "")
            source_type = "generated" if device_id.startswith("easyeda_generated:") else "external"
            placement_item = placed_symbols.get(node.refdes, {})
            symbol_id = str(placement_item.get("symbol_id") or "")
            symbol_def = symbol_defs.get(symbol_id, {})
            pin_defs = list(symbol_def.get("pins", []) or []) if isinstance(symbol_def, dict) else []
            pin_def = next(
                (
                    item
                    for item in pin_defs
                    if isinstance(item, dict)
                    and str(item.get("pin_id") or "") == str(node.pin)
                ),
                None,
            )
            orientation_record = orientation_records.get(node.refdes, {})
            out.append(
                {
                    "refdes": node.refdes,
                    "device": device_id,
                    "source_type": source_type,
                    "pin": node.pin,
                    "net": connection.net_name,
                    "component_rotation_deg": float(getattr(component, "rotation_deg", 0.0) or 0.0),
                    "component_side": str(getattr(component, "side", "") or ""),
                    "symbol_id": symbol_id,
                    "symbol_local_origin_mm": placement_item.get("symbol_local_origin_mm"),
                    "symbol_local_pin_endpoint_mm": (pin_def or {}).get("endpoint_mm"),
                    "instance_origin_mm": placement_item.get("schematic_origin_mm"),
                    "instance_rotation_deg": placement_item.get("rotation_deg"),
                    "instance_mirror": placement_item.get("mirrored"),
                    "emitted_add_command": placement_commands.get(node.refdes),
                    "emitted_rotation_token": orientation_record.get("rotation_token"),
                    "emitted_rotation_deg": orientation_record.get("rotation_deg"),
                    "transformed_anchor_mm": {"x": anchor_point[0], "y": anchor_point[1]},
                    "actual_wire_endpoint_mm": (
                        {"x": float(nearest[0]), "y": float(nearest[1])} if nearest is not None else None
                    ),
                    "endpoint_delta_mm": delta_mm,
                }
            )
    return out


def _is_ground_net_name(name: str) -> bool:
    token = "".join(ch for ch in str(name or "").upper() if ch.isalnum())
    return token in {"GND", "AGND", "DGND", "PGND", "SGND", "VSS", "VSSA", "VSSD"}


def _route_net_paths(
    net_name: str,
    mapped_nodes: list[tuple[str, str, float, float]],
    occupied_segments: list[tuple[str, tuple[float, float], tuple[float, float]]],
    placement_map: dict[str, tuple[float, float]] | None = None,
    forbidden_points: set[tuple[float, float]] | None = None,
    allow_dense_spine: bool = True,
) -> list[list[tuple[float, float]]]:
    points = _unique_points_from_nodes(mapped_nodes)
    if len(points) < 2:
        return []
    point_centers = _point_centers_for_nodes(mapped_nodes, placement_map)
    forbidden_point_index = _build_forbidden_point_index(forbidden_points)

    edge_sets = [
        _mst_edges(points),
        _chain_edges(points),
        _nearest_neighbor_chain_edges(points),
    ]

    best_paths: list[list[tuple[float, float]]] = []
    best_score: tuple[int, int, float] | None = None

    for edges in edge_sets:
        if not edges:
            continue
        paths: list[list[tuple[float, float]]] = []
        local_segments: list[tuple[str, tuple[float, float], tuple[float, float]]] = []
        complete = True
        for start, end in edges:
            path = _route_path_between_points(
                start=start,
                end=end,
                net_name=net_name,
                occupied_segments=[*occupied_segments, *local_segments],
                start_center=point_centers.get(_point_key(start)),
                end_center=point_centers.get(_point_key(end)),
                forbidden_point_index=forbidden_point_index,
            )
            if len(path) < 2:
                complete = False
                break
            paths.append(path)
            _append_occupied_segments(local_segments, net_name, path)

        if not complete or len(paths) != len(edges):
            continue

        if not paths:
            continue

        score = _net_path_quality(paths, occupied_segments)
        if best_score is None or score < best_score:
            best_score = score
            best_paths = paths

    if not best_paths:
        return []

    # Dense nets can still self-intersect even with collision scoring, which
    # triggers repetitive merge prompts in Fusion/EAGLE. Fall back to a
    # connected spine-and-branch topology for high-fanout nets.
    if (
        allow_dense_spine
        and
        placement_map is not None
        and len(points) >= 6
        and best_score is not None
        and best_score[0] > 0
    ):
        spine_paths = _dense_spine_paths_for_net(
            net_name=net_name,
            mapped_nodes=mapped_nodes,
            occupied_segments=occupied_segments,
        )
        if spine_paths:
            return spine_paths

    return best_paths


def _legacy_chain_paths_for_net(
    net_name: str,
    mapped_nodes: list[tuple[str, str, float, float]],
    occupied_segments: list[tuple[str, tuple[float, float], tuple[float, float]]],
    placement_map: dict[str, tuple[float, float]] | None = None,
    forbidden_points: set[tuple[float, float]] | None = None,
) -> list[list[tuple[float, float]]]:
    points = _unique_points_from_nodes(mapped_nodes)
    if len(points) < 2:
        return []

    point_centers = _point_centers_for_nodes(mapped_nodes, placement_map)
    forbidden_point_index = _build_forbidden_point_index(forbidden_points)
    paths: list[list[tuple[float, float]]] = []
    local_segments: list[tuple[str, tuple[float, float], tuple[float, float]]] = []
    for start, end in _chain_edges(points):
        path = _route_path_between_points(
            start=start,
            end=end,
            net_name=net_name,
            occupied_segments=[*occupied_segments, *local_segments],
            start_center=point_centers.get(_point_key(start)),
            end_center=point_centers.get(_point_key(end)),
            forbidden_point_index=forbidden_point_index,
        )
        if len(path) < 2:
            return []
        paths.append(path)
        _append_occupied_segments(local_segments, net_name, path)
    return paths


def _dense_spine_paths_for_net(
    net_name: str,
    mapped_nodes: list[tuple[str, str, float, float]],
    occupied_segments: list[tuple[str, tuple[float, float], tuple[float, float]]],
) -> list[list[tuple[float, float]]]:
    points = _unique_points_from_nodes(mapped_nodes)
    if len(points) < 2:
        return []

    xs = [pt[0] for pt in points]
    min_x = min(xs)
    max_x = max(xs)

    candidate_spines = [
        min_x - 2.54,
        min_x - 5.08,
        min_x - 7.62,
        max_x + 2.54,
        max_x + 5.08,
        max_x + 7.62,
    ]

    best_paths: list[list[tuple[float, float]]] = []
    best_score: tuple[int, int, float] | None = None
    for spine_x in candidate_spines:
        paths = _build_spine_candidate_paths(points, spine_x)
        if not paths:
            continue
        score = _net_path_quality(paths, occupied_segments)
        if best_score is None or score < best_score:
            best_score = score
            best_paths = paths

    return best_paths


def _build_spine_candidate_paths(
    points: list[tuple[float, float]],
    spine_x: float,
) -> list[list[tuple[float, float]]]:
    if len(points) < 2:
        return []

    lane_pitch = 1.27
    ordered = sorted(points, key=lambda item: (round(item[1], 4), round(item[0], 4)))
    lanes: list[float] = []
    node_lanes: list[tuple[tuple[float, float], float]] = []
    for x, y in ordered:
        lane_y = float(y)
        guard = 0
        while any(abs(lane_y - existing) < 0.2 for existing in lanes):
            guard += 1
            if guard > 50:
                break
            lane_y = float(y) + guard * lane_pitch
        lanes.append(lane_y)
        node_lanes.append(((x, y), lane_y))

    paths: list[list[tuple[float, float]]] = []
    for ordinal, ((x, y), lane_y) in enumerate(node_lanes, start=1):
        if math.isclose(y, lane_y, abs_tol=1e-6):
            branch = _dedupe_consecutive_points([(x, y), (spine_x, lane_y)])
        else:
            side_sign = -1.0 if spine_x <= x else 1.0
            jog_x = spine_x + side_sign * (0.635 * float(ordinal))
            branch = _dedupe_consecutive_points(
                [
                    (x, y),
                    (spine_x, y),
                    (jog_x, y),
                    (jog_x, lane_y),
                    (spine_x, lane_y),
                ]
            )
        if len(branch) >= 2:
            paths.append(branch)

    sorted_lanes = sorted(lanes)
    if len(sorted_lanes) >= 2:
        for idx in range(len(sorted_lanes) - 1):
            y1 = sorted_lanes[idx]
            y2 = sorted_lanes[idx + 1]
            if math.isclose(y1, y2, abs_tol=1e-6):
                continue
            spine_segment = _dedupe_consecutive_points([(spine_x, y1), (spine_x, y2)])
            if len(spine_segment) >= 2:
                paths.append(spine_segment)

    return paths


def _chain_edges(points: list[tuple[float, float]]) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    if len(points) < 2:
        return []
    ordered = sorted(points, key=lambda item: (round(item[0], 4), round(item[1], 4)))
    return [(ordered[idx], ordered[idx + 1]) for idx in range(len(ordered) - 1)]


def _nearest_neighbor_chain_edges(
    points: list[tuple[float, float]],
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    if len(points) < 2:
        return []

    remaining = list(points)
    start = min(remaining, key=lambda item: (round(item[0], 4), round(item[1], 4)))
    remaining.remove(start)
    ordered = [start]
    current = start
    while remaining:
        nxt = min(
            remaining,
            key=lambda item: (
                _manhattan_distance(current, item),
                round(item[0], 4),
                round(item[1], 4),
            ),
        )
        ordered.append(nxt)
        remaining.remove(nxt)
        current = nxt

    return [(ordered[idx], ordered[idx + 1]) for idx in range(len(ordered) - 1)]


def _net_path_quality(
    paths: list[list[tuple[float, float]]],
    occupied_segments: list[tuple[str, tuple[float, float], tuple[float, float]]],
) -> tuple[int, int, float]:
    segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    total_length = 0.0
    for path in paths:
        path_segments = _path_segments(path)
        segments.extend(path_segments)
        for start, end in path_segments:
            total_length += _manhattan_distance(start, end)

    internal_intersections = 0
    for idx in range(len(segments)):
        a_start, a_end = segments[idx]
        for jdx in range(idx + 1, len(segments)):
            b_start, b_end = segments[jdx]
            if _segments_share_endpoint(a_start, a_end, b_start, b_end):
                continue
            if _axis_segments_touch(a_start, a_end, b_start, b_end):
                internal_intersections += 1

    external_touches = 0
    occupied_index = _build_occupied_segment_index(occupied_segments)
    for start, end in segments:
        for segment in _iter_touching_occupied_segments(start, end, occupied_index):
            if _segments_share_endpoint(start, end, segment.start, segment.end):
                continue
            external_touches += 1

    return (internal_intersections, external_touches, total_length)


def _stub_paths_for_net(
    net_name: str,
    mapped_nodes: list[tuple[str, str, float, float]],
    placement_map: dict[str, tuple[float, float]],
    occupied_segments: list[tuple[str, tuple[float, float], tuple[float, float]]],
    forbidden_points: set[tuple[float, float]] | None = None,
) -> list[list[tuple[float, float]]]:
    unique_nodes: list[tuple[str, str, float, float]] = []
    seen_points: set[tuple[float, float]] = set()
    for ref, pin, x, y in mapped_nodes:
        key = (round(x, 4), round(y, 4))
        if key in seen_points:
            continue
        seen_points.add(key)
        unique_nodes.append((ref, pin, x, y))

    if len(unique_nodes) < 2:
        return []
    forbidden_point_index = _build_forbidden_point_index(forbidden_points)

    local_occupied: list[tuple[str, tuple[float, float], tuple[float, float]]] = []
    paths: list[list[tuple[float, float]]] = []
    for idx, (ref, _pin, x, y) in enumerate(unique_nodes):
        center = placement_map.get(ref, (x, y))
        candidates = _stub_candidates_for_anchor(
            anchor=(x, y),
            center=center,
            ordinal=idx,
        )
        best: tuple[int, float, list[tuple[float, float]]] | None = None
        candidate_occupied = [*occupied_segments, *local_occupied]
        candidate_index = _build_occupied_segment_index(candidate_occupied)
        for path in candidates:
            score = _path_collision_score(
                path,
                net_name,
                candidate_occupied,
                forbidden_point_index=forbidden_point_index,
                occupied_index=candidate_index,
            )
            length = _path_total_length(path)
            ranked = (score, length, path)
            if best is None or ranked < best:
                best = ranked
        if best is None:
            continue
        path = best[2]
        paths.append(path)
        _append_occupied_segments(local_occupied, net_name, path)

    return paths


def _stub_candidates_for_anchor(
    anchor: tuple[float, float],
    center: tuple[float, float],
    ordinal: int,
) -> list[list[tuple[float, float]]]:
    x, y = anchor
    cx, cy = center
    # Keep stubs short to avoid crossing nearby pins/nets on dense symbols.
    lengths = [1.27, 1.905, 2.54]

    if abs(x - cx) >= abs(y - cy):
        preferred = [(1.0, 0.0)] if x >= cx else [(-1.0, 0.0)]
    else:
        preferred = [(0.0, 1.0)] if y >= cy else [(0.0, -1.0)]

    all_dirs = preferred + [(-1.0, 0.0), (1.0, 0.0), (0.0, -1.0), (0.0, 1.0)]
    ordered_dirs: list[tuple[float, float]] = []
    for direction in all_dirs:
        if direction not in ordered_dirs:
            ordered_dirs.append(direction)

    out: list[list[tuple[float, float]]] = []
    for length in lengths:
        for dx, dy in ordered_dirs:
            # Tiny deterministic delta prevents identical overlap without
            # extending stubs far enough to cut across neighboring pins.
            effective_length = length + float(ordinal % 4) * 0.05
            if abs(dx) > 0.0:
                ex = x + dx * effective_length
                ey = y
            else:
                ex = x
                ey = y + dy * effective_length
            out.append(_dedupe_consecutive_points([(x, y), (ex, ey)]))
    return out


def _attach_pin_anchors_to_paths(
    net_paths: list[list[tuple[float, float]]],
    mapped_nodes: list[tuple[str, str, float, float]],
    resolved_anchor_by_ref_pin: dict[tuple[str, str], _ResolvedPinAnchor],
    placement_map: dict[str, tuple[float, float]],
    stub_length_mm: float,
    snap_to_default_grid: bool,
) -> list[list[tuple[float, float]]]:
    if not net_paths or not mapped_nodes:
        return net_paths

    stub_by_anchor: dict[tuple[float, float], tuple[float, float]] = {}
    for ref, pin, x_mm, y_mm in mapped_nodes:
        key = (ref, pin)
        anchor = resolved_anchor_by_ref_pin.get(key)
        if anchor is None:
            anchor = _fallback_anchor_from_component_center(placement_map, ref, x_mm, y_mm)
        direction = _normalize_axis_direction(anchor.outward_dx, anchor.outward_dy)
        if direction == (0.0, 0.0):
            cx, cy = placement_map.get(ref, (anchor.x_mm, anchor.y_mm))
            direction = _direction_from_offset(anchor.x_mm - cx, anchor.y_mm - cy)
        if direction == (0.0, 0.0):
            continue
        stub = (
            anchor.x_mm + direction[0] * float(stub_length_mm),
            anchor.y_mm + direction[1] * float(stub_length_mm),
        )
        if snap_to_default_grid:
            stub = _snap_point_to_schematic_grid(stub)
        anchor_point = (anchor.x_mm, anchor.y_mm)
        if snap_to_default_grid:
            anchor_point = _snap_point_to_schematic_grid(anchor_point)
        if _point_key(anchor_point) == _point_key(stub):
            continue
        stub_by_anchor[_point_key(anchor_point)] = stub

    if not stub_by_anchor:
        return net_paths

    remapped_paths: list[list[tuple[float, float]]] = []
    for path in net_paths:
        if len(path) < 2:
            continue
        mapped = list(path)
        start_key = _point_key(mapped[0])
        end_key = _point_key(mapped[-1])
        if start_key in stub_by_anchor:
            mapped[0] = stub_by_anchor[start_key]
        if end_key in stub_by_anchor:
            mapped[-1] = stub_by_anchor[end_key]
        mapped = _dedupe_consecutive_points(mapped)
        if len(mapped) >= 2:
            remapped_paths.append(mapped)

    if not remapped_paths:
        remapped_paths = list(net_paths)

    # Emit explicit anchor->stub segments so the wire visibly starts at the
    # true pin anchor and exits in the pin's natural outward direction.
    for anchor_key, stub in sorted(stub_by_anchor.items()):
        anchor = (float(anchor_key[0]), float(anchor_key[1]))
        remapped_paths.append([anchor, stub])

    return remapped_paths


def _unique_points_from_nodes(mapped_nodes: list[tuple[str, str, float, float]]) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    seen: set[tuple[float, float]] = set()
    for _, _, x, y in mapped_nodes:
        key = _point_key((x, y))
        if key in seen:
            continue
        seen.add(key)
        points.append((x, y))
    return points


def _build_net_connection_map(
    effective_nets: list[Net],
    refdes_map: dict[str, str],
    placed_refs: set[str],
    valid_pins_by_ref: dict[str, set[str]],
    resolved_anchor_by_ref_pin: dict[tuple[str, str], _ResolvedPinAnchor],
    placement_map: dict[str, tuple[float, float]],
) -> list[_NetConnection]:
    out: list[_NetConnection] = []
    for net in effective_nets:
        seen_nodes: set[tuple[str, str]] = set()
        nodes: list[_NetConnectionNode] = []
        for raw_node in net.nodes:
            ref = refdes_map.get(raw_node.refdes, _sanitize_refdes(raw_node.refdes))
            if ref not in placed_refs:
                continue
            pin = str(raw_node.pin).strip()
            if not pin:
                continue
            valid_pins = valid_pins_by_ref.get(ref, set())
            if valid_pins and pin not in valid_pins:
                continue
            key = (ref, pin)
            if key in seen_nodes:
                continue
            seen_nodes.add(key)
            anchor = resolved_anchor_by_ref_pin.get(key)
            if anchor is None:
                fallback_x, fallback_y = placement_map.get(ref, (0.0, 0.0))
                anchor = _fallback_anchor_from_component_center(
                    placement_map=placement_map,
                    refdes=ref,
                    fallback_x=fallback_x,
                    fallback_y=fallback_y,
                )
            nodes.append(_NetConnectionNode(refdes=ref, pin=pin, anchor=anchor))
        if not nodes:
            continue
        nodes_sorted = tuple(sorted(nodes, key=lambda item: (item.refdes, item.pin)))
        out.append(_NetConnection(net_name=str(net.name), nodes=nodes_sorted))
    return out


def _should_draw_net_with_stub_labels(
    net_name: str,
    mapped_nodes: list[tuple[str, str, float, float]],
    placement_map: dict[str, tuple[float, float]],
) -> bool:
    if len(mapped_nodes) < 2:
        return False
    if _normalize_power_net_name(net_name):
        return True

    xs = [x for _, _, x, _ in mapped_nodes]
    ys = [y for _, _, _, y in mapped_nodes]
    spread = math.hypot(max(xs) - min(xs), max(ys) - min(ys))

    if len(mapped_nodes) >= 6:
        return True
    if len(mapped_nodes) >= 4 and spread >= 55.0:
        return True
    if len(mapped_nodes) >= 3 and spread >= 90.0:
        return True

    # Heuristic: if most nodes are attached to different component centers and
    # the net is physically spread out, prefer compact label stubs.
    centers: set[tuple[float, float]] = set()
    for refdes, _, _, _ in mapped_nodes:
        cx, cy = placement_map.get(refdes, (0.0, 0.0))
        centers.add(_point_key((cx, cy)))
    return len(centers) >= 5 and spread >= 40.0


def _build_stub_label_paths_for_net(
    net_name: str,
    mapped_nodes: list[tuple[str, str, float, float]],
    resolved_anchor_by_ref_pin: dict[tuple[str, str], _ResolvedPinAnchor],
    placement_map: dict[str, tuple[float, float]],
    stub_length_mm: float,
    occupied_segments: list[tuple[str, tuple[float, float], tuple[float, float]]] | None = None,
    forbidden_points: set[tuple[float, float]] | None = None,
) -> list[PlannedNetPath]:
    out: list[PlannedNetPath] = []
    seen_anchor_keys: set[tuple[float, float]] = set()
    local_segments: list[tuple[str, tuple[float, float], tuple[float, float]]] = []
    occupied_now = list(occupied_segments or [])
    forbidden_point_index = _build_forbidden_point_index(forbidden_points)
    for refdes, pin, x_mm, y_mm in mapped_nodes:
        key = (refdes, pin)
        anchor = resolved_anchor_by_ref_pin.get(key)
        if anchor is None:
            anchor = _fallback_anchor_from_component_center(
                placement_map=placement_map,
                refdes=refdes,
                fallback_x=x_mm,
                fallback_y=y_mm,
            )
        direction = _normalize_axis_direction(anchor.outward_dx, anchor.outward_dy)
        if direction == (0.0, 0.0):
            cx, cy = placement_map.get(refdes, (anchor.x_mm, anchor.y_mm))
            direction = _direction_from_offset(anchor.x_mm - cx, anchor.y_mm - cy)
        if direction == (0.0, 0.0):
            direction = (1.0, 0.0)

        start = (anchor.x_mm, anchor.y_mm)
        chosen_path: list[tuple[float, float]] | None = None
        best_fallback: tuple[int, float, list[tuple[float, float]]] | None = None
        candidate_occupied = [*occupied_now, *local_segments]
        candidate_index = _build_occupied_segment_index(candidate_occupied)
        for cand_dx, cand_dy in _stub_direction_candidates(direction):
            for length_scale in (1.0, 2.0, 3.0):
                length = float(stub_length_mm) * float(length_scale)
                end = (
                    anchor.x_mm + cand_dx * length,
                    anchor.y_mm + cand_dy * length,
                )
                if _point_key(start) == _point_key(end):
                    continue
                candidate = _dedupe_consecutive_points([start, end])
                if len(candidate) < 2:
                    continue
                if not _path_has_hard_conflict(
                    path=candidate,
                    net_name=net_name,
                    occupied_segments=candidate_occupied,
                    forbidden_point_index=forbidden_point_index,
                    occupied_index=candidate_index,
                ):
                    chosen_path = candidate
                    break
                candidate_score = _path_collision_score(
                    candidate,
                    net_name,
                    candidate_occupied,
                    forbidden_point_index=forbidden_point_index,
                    occupied_index=candidate_index,
                )
                fallback_rank = (
                    candidate_score,
                    _path_total_length(candidate),
                    candidate,
                )
                if best_fallback is None or fallback_rank[:2] < best_fallback[:2]:
                    best_fallback = fallback_rank
            if chosen_path is not None:
                break
        if chosen_path is None and best_fallback is not None:
            chosen_path = best_fallback[2]
        if chosen_path is None:
            continue
        start_key = _point_key(start)
        if start_key in seen_anchor_keys:
            continue
        seen_anchor_keys.add(start_key)
        out.append(PlannedNetPath(points=tuple(chosen_path), owner_refdes=refdes, owner_pin=pin))
        _append_occupied_segments(local_segments, net_name, chosen_path)
    return out


def _stub_direction_candidates(
    primary: tuple[float, float],
) -> list[tuple[float, float]]:
    base = _normalize_axis_direction(primary[0], primary[1])
    if base == (0.0, 0.0):
        base = (1.0, 0.0)
    perp_a = _normalize_axis_direction(-base[1], base[0])
    perp_b = _normalize_axis_direction(base[1], -base[0])
    opposite = _normalize_axis_direction(-base[0], -base[1])

    ordered = [base, perp_a, perp_b, opposite]
    unique: list[tuple[float, float]] = []
    seen: set[tuple[float, float]] = set()
    for direction in ordered:
        if direction == (0.0, 0.0):
            continue
        if direction in seen:
            continue
        seen.add(direction)
        unique.append(direction)
    return unique


def _point_key(point: tuple[float, float]) -> tuple[float, float]:
    return (round(float(point[0]), 4), round(float(point[1]), 4))


def _all_anchor_points(
    anchor_map: dict[str, dict[str, _ResolvedPinAnchor]],
) -> set[tuple[float, float]]:
    points: set[tuple[float, float]] = set()
    for pin_map in anchor_map.values():
        for anchor in pin_map.values():
            points.add(_point_key((anchor.x_mm, anchor.y_mm)))
    return points


def _build_forbidden_point_index(
    points: set[tuple[float, float]] | None,
) -> tuple[dict[float, tuple[float, ...]], dict[float, tuple[float, ...]]] | None:
    if not points:
        return None
    by_x_raw: dict[float, list[float]] = defaultdict(list)
    by_y_raw: dict[float, list[float]] = defaultdict(list)
    for x_mm, y_mm in points:
        x_key = round(float(x_mm), 4)
        y_key = round(float(y_mm), 4)
        by_x_raw[x_key].append(y_key)
        by_y_raw[y_key].append(x_key)

    by_x: dict[float, tuple[float, ...]] = {
        x_key: tuple(sorted(set(vals)))
        for x_key, vals in by_x_raw.items()
    }
    by_y: dict[float, tuple[float, ...]] = {
        y_key: tuple(sorted(set(vals)))
        for y_key, vals in by_y_raw.items()
    }
    return by_x, by_y


def _point_centers_for_nodes(
    mapped_nodes: list[tuple[str, str, float, float]],
    placement_map: dict[str, tuple[float, float]] | None,
) -> dict[tuple[float, float], tuple[float, float]]:
    if not placement_map:
        return {}
    out: dict[tuple[float, float], tuple[float, float]] = {}
    for ref, _pin, x_mm, y_mm in mapped_nodes:
        key = _point_key((x_mm, y_mm))
        center = placement_map.get(ref)
        if center is None:
            continue
        out.setdefault(key, center)
    return out


def _mst_edges(points: list[tuple[float, float]]) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    if len(points) < 2:
        return []
    visited = {0}
    edges: list[tuple[tuple[float, float], tuple[float, float]]] = []
    while len(visited) < len(points):
        best: tuple[float, int, int] | None = None
        for left_idx in visited:
            for right_idx in range(len(points)):
                if right_idx in visited:
                    continue
                dist = _manhattan_distance(points[left_idx], points[right_idx])
                candidate = (dist, left_idx, right_idx)
                if best is None or candidate < best:
                    best = candidate
        if best is None:
            break
        _, left_idx, right_idx = best
        visited.add(right_idx)
        edges.append((points[left_idx], points[right_idx]))
    return edges


@dataclass(frozen=True)
class _IndexedOccupiedSegment:
    net_name: str
    start: tuple[float, float]
    end: tuple[float, float]
    axis_key: float
    low: float
    high: float


@dataclass(frozen=True)
class _OccupiedSegmentIndex:
    horizontal_by_y: dict[float, tuple[_IndexedOccupiedSegment, ...]]
    vertical_by_x: dict[float, tuple[_IndexedOccupiedSegment, ...]]
    horizontal_keys: tuple[float, ...]
    vertical_keys: tuple[float, ...]


def _axis_key(value: float) -> float:
    return round(float(value), 4)


def _build_occupied_segment_index(
    occupied_segments: list[tuple[str, tuple[float, float], tuple[float, float]]],
    eps: float = 1e-6,
) -> _OccupiedSegmentIndex:
    horizontal_raw: dict[float, list[_IndexedOccupiedSegment]] = {}
    vertical_raw: dict[float, list[_IndexedOccupiedSegment]] = {}

    for net_name, start, end in occupied_segments:
        sx, sy = start
        ex, ey = end
        if math.isclose(sx, ex, abs_tol=eps):
            xk = _axis_key(sx)
            low = min(sy, ey)
            high = max(sy, ey)
            vertical_raw.setdefault(xk, []).append(
                _IndexedOccupiedSegment(
                    net_name=str(net_name),
                    start=start,
                    end=end,
                    axis_key=xk,
                    low=low,
                    high=high,
                )
            )
            continue
        if math.isclose(sy, ey, abs_tol=eps):
            yk = _axis_key(sy)
            low = min(sx, ex)
            high = max(sx, ex)
            horizontal_raw.setdefault(yk, []).append(
                _IndexedOccupiedSegment(
                    net_name=str(net_name),
                    start=start,
                    end=end,
                    axis_key=yk,
                    low=low,
                    high=high,
                )
            )

    horizontal_by_y = {
        key: tuple(items)
        for key, items in horizontal_raw.items()
    }
    vertical_by_x = {
        key: tuple(items)
        for key, items in vertical_raw.items()
    }
    horizontal_keys = tuple(sorted(horizontal_by_y.keys()))
    vertical_keys = tuple(sorted(vertical_by_x.keys()))
    return _OccupiedSegmentIndex(
        horizontal_by_y=horizontal_by_y,
        vertical_by_x=vertical_by_x,
        horizontal_keys=horizontal_keys,
        vertical_keys=vertical_keys,
    )


def _axis_keys_between(
    keys: tuple[float, ...],
    low: float,
    high: float,
    eps: float = 1e-6,
) -> tuple[float, ...]:
    if not keys:
        return ()
    lo = min(low, high) - eps
    hi = max(low, high) + eps
    left_idx = bisect.bisect_left(keys, lo)
    right_idx = bisect.bisect_right(keys, hi)
    if left_idx >= right_idx:
        return ()
    return keys[left_idx:right_idx]


def _iter_touching_occupied_segments(
    start: tuple[float, float],
    end: tuple[float, float],
    index: _OccupiedSegmentIndex,
    eps: float = 1e-6,
) -> Iterable[_IndexedOccupiedSegment]:
    sx, sy = start
    ex, ey = end
    vertical = math.isclose(sx, ex, abs_tol=eps)

    if vertical:
        x_key = _axis_key(sx)
        y0 = min(sy, ey)
        y1 = max(sy, ey)
        for segment in index.vertical_by_x.get(x_key, ()):
            if max(y0, segment.low) <= min(y1, segment.high) + eps:
                yield segment
        for y_key in _axis_keys_between(index.horizontal_keys, y0, y1, eps):
            for segment in index.horizontal_by_y.get(y_key, ()):
                if segment.low - eps <= sx <= segment.high + eps:
                    yield segment
        return

    y_key = _axis_key(sy)
    x0 = min(sx, ex)
    x1 = max(sx, ex)
    for segment in index.horizontal_by_y.get(y_key, ()):
        if max(x0, segment.low) <= min(x1, segment.high) + eps:
            yield segment
    for x_key in _axis_keys_between(index.vertical_keys, x0, x1, eps):
        for segment in index.vertical_by_x.get(x_key, ()):
            if segment.low - eps <= sy <= segment.high + eps:
                yield segment


def _route_path_between_points(
    start: tuple[float, float],
    end: tuple[float, float],
    net_name: str,
    occupied_segments: list[tuple[str, tuple[float, float], tuple[float, float]]],
    start_center: tuple[float, float] | None = None,
    end_center: tuple[float, float] | None = None,
    forbidden_point_index: tuple[dict[float, tuple[float, ...]], dict[float, tuple[float, ...]]] | None = None,
) -> list[tuple[float, float]]:
    candidates = _manhattan_path_candidates(
        start,
        end,
        start_center=start_center,
        end_center=end_center,
    )
    if not candidates:
        return []

    occupied_index = _build_occupied_segment_index(occupied_segments)
    ranked_candidates: list[tuple[int, int, float, list[tuple[float, float]], bool]] = []
    for path in candidates:
        collision_score, hard_conflict = _path_collision_metrics(
            path=path,
            net_name=net_name,
            occupied_segments=occupied_segments,
            forbidden_point_index=forbidden_point_index,
            occupied_index=occupied_index,
        )
        if hard_conflict:
            continue
        perpendicular_penalty = _endpoint_perpendicular_penalty(
            path=path,
            start=start,
            end=end,
            start_center=start_center,
            end_center=end_center,
        )
        route_length = _path_total_length(path)
        ranked_candidates.append(
            (collision_score, perpendicular_penalty, route_length, path, hard_conflict)
        )

    ranked_candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    for _, _, _, path, hard_conflict in ranked_candidates:
        if hard_conflict:
            continue
        return path
    return []


def _manhattan_path_candidates(
    start: tuple[float, float],
    end: tuple[float, float],
    start_center: tuple[float, float] | None = None,
    end_center: tuple[float, float] | None = None,
) -> list[list[tuple[float, float]]]:
    out: list[list[tuple[float, float]]] = list(_manhattan_core_path_candidates(start, end))
    stub_lengths_mm = (_SCHEMATIC_DEFAULT_GRID_MM, _SCHEMATIC_DEFAULT_GRID_MM * 2.0)

    start_dir = _preferred_pin_exit_direction(start, start_center)
    end_dir = _preferred_pin_exit_direction(end, end_center)

    if start_dir is not None:
        for stub_len in stub_lengths_mm:
            start_stub = (
                start[0] + start_dir[0] * stub_len,
                start[1] + start_dir[1] * stub_len,
            )
            for core in _manhattan_core_path_candidates(start_stub, end):
                out.append(_dedupe_consecutive_points([start, *core]))

    if end_dir is not None:
        for stub_len in stub_lengths_mm:
            end_stub = (
                end[0] + end_dir[0] * stub_len,
                end[1] + end_dir[1] * stub_len,
            )
            for core in _manhattan_core_path_candidates(start, end_stub):
                out.append(_dedupe_consecutive_points([*core, end]))

    if start_dir is not None and end_dir is not None:
        for start_stub_len in stub_lengths_mm:
            start_stub = (
                start[0] + start_dir[0] * start_stub_len,
                start[1] + start_dir[1] * start_stub_len,
            )
            for end_stub_len in stub_lengths_mm:
                end_stub = (
                    end[0] + end_dir[0] * end_stub_len,
                    end[1] + end_dir[1] * end_stub_len,
                )
                for core in _manhattan_core_path_candidates(start_stub, end_stub):
                    if len(core) < 2:
                        continue
                    mid = core[1:-1]
                    out.append(_dedupe_consecutive_points([start, start_stub, *mid, end_stub, end]))

    unique: list[list[tuple[float, float]]] = []
    seen: set[tuple[tuple[float, float], ...]] = set()
    for path in out:
        if len(path) < 2:
            continue
        if not _is_orthogonal_path(path):
            continue
        key = tuple((round(x, 4), round(y, 4)) for x, y in path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _manhattan_core_path_candidates(
    start: tuple[float, float],
    end: tuple[float, float],
) -> list[list[tuple[float, float]]]:
    sx, sy = start
    ex, ey = end
    out: list[list[tuple[float, float]]] = []
    offsets = [1.27 * idx for idx in range(1, 15)]

    if math.isclose(sx, ex, abs_tol=1e-6) or math.isclose(sy, ey, abs_tol=1e-6):
        out.append(_dedupe_consecutive_points([start, end]))

    out.append(_dedupe_consecutive_points([start, (ex, sy), end]))
    out.append(_dedupe_consecutive_points([start, (sx, ey), end]))

    for delta in offsets:
        for sign in (-1.0, 1.0):
            xmid = sx + sign * delta
            ymid = sy + sign * delta
            out.append(_dedupe_consecutive_points([start, (xmid, sy), (xmid, ey), end]))
            out.append(_dedupe_consecutive_points([start, (sx, ymid), (ex, ymid), end]))

            xmid_e = ex + sign * delta
            ymid_e = ey + sign * delta
            out.append(_dedupe_consecutive_points([start, (xmid_e, sy), (xmid_e, ey), end]))
            out.append(_dedupe_consecutive_points([start, (sx, ymid_e), (ex, ymid_e), end]))

    return out


def _preferred_pin_exit_direction(
    point: tuple[float, float],
    center: tuple[float, float] | None,
) -> tuple[int, int] | None:
    if center is None:
        return None
    dx = float(point[0]) - float(center[0])
    dy = float(point[1]) - float(center[1])
    if math.isclose(dx, 0.0, abs_tol=1e-6) and math.isclose(dy, 0.0, abs_tol=1e-6):
        return None
    if abs(dx) >= abs(dy):
        return (1, 0) if dx >= 0.0 else (-1, 0)
    return (0, 1) if dy >= 0.0 else (0, -1)


def _endpoint_perpendicular_penalty(
    path: list[tuple[float, float]],
    start: tuple[float, float],
    end: tuple[float, float],
    start_center: tuple[float, float] | None,
    end_center: tuple[float, float] | None,
) -> int:
    if len(path) < 2:
        return 100

    penalty = 0
    start_dir = _preferred_pin_exit_direction(start, start_center)
    if start_dir is not None:
        seg_dir = _axis_segment_direction(start, path[1])
        if seg_dir is None or seg_dir != start_dir:
            penalty += 10

    end_dir = _preferred_pin_exit_direction(end, end_center)
    if end_dir is not None:
        seg_dir = _axis_segment_direction(end, path[-2])
        if seg_dir is None or seg_dir != end_dir:
            penalty += 10

    return penalty


def _axis_segment_direction(
    start: tuple[float, float],
    end: tuple[float, float],
) -> tuple[int, int] | None:
    sx, sy = start
    ex, ey = end
    if math.isclose(sx, ex, abs_tol=1e-6):
        if math.isclose(sy, ey, abs_tol=1e-6):
            return None
        return (0, 1) if ey > sy else (0, -1)
    if math.isclose(sy, ey, abs_tol=1e-6):
        return (1, 0) if ex > sx else (-1, 0)
    return None


def _is_orthogonal_path(path: list[tuple[float, float]]) -> bool:
    if len(path) < 2:
        return False
    for idx in range(len(path) - 1):
        x1, y1 = path[idx]
        x2, y2 = path[idx + 1]
        if not (math.isclose(x1, x2, abs_tol=1e-6) or math.isclose(y1, y2, abs_tol=1e-6)):
            return False
    return True


def _path_collision_metrics(
    path: list[tuple[float, float]],
    net_name: str,
    occupied_segments: list[tuple[str, tuple[float, float], tuple[float, float]]],
    forbidden_points: set[tuple[float, float]] | None = None,
    forbidden_point_index: tuple[dict[float, tuple[float, ...]], dict[float, tuple[float, ...]]] | None = None,
    occupied_index: _OccupiedSegmentIndex | None = None,
) -> tuple[int, bool]:
    score = 0
    hard_conflict = False
    path_segments = _path_segments(path)
    if forbidden_point_index is None:
        forbidden_point_index = _build_forbidden_point_index(forbidden_points)
    if occupied_index is None:
        occupied_index = _build_occupied_segment_index(occupied_segments)
    for left in path_segments:
        for segment in _iter_touching_occupied_segments(left[0], left[1], occupied_index):
            if segment.net_name == net_name:
                if _segments_share_endpoint(left[0], left[1], segment.start, segment.end):
                    continue
                # Same-net overlap still triggers interactive merge prompts in Fusion/EAGLE.
                score += 600
                continue
            score += 400
            hard_conflict = True
        if forbidden_point_index and _axis_segment_hits_forbidden_anchor(left[0], left[1], forbidden_point_index):
            # Crossing another component pin anchor is the primary source of
            # cross-net merge prompts in Fusion/EAGLE.
            score += 900
            hard_conflict = True
    return score, hard_conflict


def _path_collision_score(
    path: list[tuple[float, float]],
    net_name: str,
    occupied_segments: list[tuple[str, tuple[float, float], tuple[float, float]]],
    forbidden_points: set[tuple[float, float]] | None = None,
    forbidden_point_index: tuple[dict[float, tuple[float, ...]], dict[float, tuple[float, ...]]] | None = None,
    occupied_index: _OccupiedSegmentIndex | None = None,
) -> int:
    score, _ = _path_collision_metrics(
        path=path,
        net_name=net_name,
        occupied_segments=occupied_segments,
        forbidden_points=forbidden_points,
        forbidden_point_index=forbidden_point_index,
        occupied_index=occupied_index,
    )
    return score


def _path_has_hard_conflict(
    path: list[tuple[float, float]],
    net_name: str,
    occupied_segments: list[tuple[str, tuple[float, float], tuple[float, float]]],
    forbidden_points: set[tuple[float, float]] | None = None,
    forbidden_point_index: tuple[dict[float, tuple[float, ...]], dict[float, tuple[float, ...]]] | None = None,
    occupied_index: _OccupiedSegmentIndex | None = None,
) -> bool:
    _, hard_conflict = _path_collision_metrics(
        path=path,
        net_name=net_name,
        occupied_segments=occupied_segments,
        forbidden_points=forbidden_points,
        forbidden_point_index=forbidden_point_index,
        occupied_index=occupied_index,
    )
    return hard_conflict


def _axis_segment_hits_forbidden_anchor(
    start: tuple[float, float],
    end: tuple[float, float],
    forbidden_point_index: tuple[dict[float, tuple[float, ...]], dict[float, tuple[float, ...]]],
    eps: float = 1e-6,
) -> bool:
    by_x, by_y = forbidden_point_index
    sx, sy = start
    ex, ey = end
    if math.isclose(sx, ex, abs_tol=eps):
        x_key = round(float(sx), 4)
        y_values = by_x.get(x_key)
        if not y_values:
            return False
        y0 = min(round(float(sy), 4), round(float(ey), 4))
        y1 = max(round(float(sy), 4), round(float(ey), 4))
        left_idx = bisect.bisect_left(y_values, y0 - eps)
        right_idx = bisect.bisect_right(y_values, y1 + eps)
        return left_idx < right_idx
    if math.isclose(sy, ey, abs_tol=eps):
        y_key = round(float(sy), 4)
        x_values = by_y.get(y_key)
        if not x_values:
            return False
        x0 = min(round(float(sx), 4), round(float(ex), 4))
        x1 = max(round(float(sx), 4), round(float(ex), 4))
        left_idx = bisect.bisect_left(x_values, x0 - eps)
        right_idx = bisect.bisect_right(x_values, x1 + eps)
        return left_idx < right_idx
    return False


def _segments_share_endpoint(
    a_start: tuple[float, float],
    a_end: tuple[float, float],
    b_start: tuple[float, float],
    b_end: tuple[float, float],
    eps: float = 1e-6,
) -> bool:
    endpoints_a = (a_start, a_end)
    endpoints_b = (b_start, b_end)
    for ax, ay in endpoints_a:
        for bx, by in endpoints_b:
            if math.isclose(ax, bx, abs_tol=eps) and math.isclose(ay, by, abs_tol=eps):
                return True
    return False


def _path_segments(path: list[tuple[float, float]]) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    out: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for idx in range(len(path) - 1):
        start = path[idx]
        end = path[idx + 1]
        if math.isclose(start[0], end[0], abs_tol=1e-6) and math.isclose(start[1], end[1], abs_tol=1e-6):
            continue
        out.append((start, end))
    return out


def _axis_segments_touch(
    a_start: tuple[float, float],
    a_end: tuple[float, float],
    b_start: tuple[float, float],
    b_end: tuple[float, float],
    eps: float = 1e-6,
) -> bool:
    ax1, ay1 = a_start
    ax2, ay2 = a_end
    bx1, by1 = b_start
    bx2, by2 = b_end
    a_vertical = abs(ax1 - ax2) <= eps
    b_vertical = abs(bx1 - bx2) <= eps

    if a_vertical and b_vertical:
        if abs(ax1 - bx1) > eps:
            return False
        a0 = min(ay1, ay2)
        a1 = max(ay1, ay2)
        b0 = min(by1, by2)
        b1 = max(by1, by2)
        return max(a0, b0) <= min(a1, b1) + eps

    if not a_vertical and not b_vertical:
        if abs(ay1 - by1) > eps:
            return False
        a0 = min(ax1, ax2)
        a1 = max(ax1, ax2)
        b0 = min(bx1, bx2)
        b1 = max(bx1, bx2)
        return max(a0, b0) <= min(a1, b1) + eps

    if a_vertical:
        x = ax1
        y = by1
        bx0 = min(bx1, bx2)
        bx1s = max(bx1, bx2)
        ay0 = min(ay1, ay2)
        ay1s = max(ay1, ay2)
        return bx0 - eps <= x <= bx1s + eps and ay0 - eps <= y <= ay1s + eps

    x = bx1
    y = ay1
    ax0 = min(ax1, ax2)
    ax1s = max(ax1, ax2)
    by0 = min(by1, by2)
    by1s = max(by1, by2)
    return ax0 - eps <= x <= ax1s + eps and by0 - eps <= y <= by1s + eps


def _append_occupied_segments(
    occupied_segments: list[tuple[str, tuple[float, float], tuple[float, float]]],
    net_name: str,
    path: list[tuple[float, float]],
) -> None:
    for start, end in _path_segments(path):
        occupied_segments.append((net_name, start, end))


def _path_total_length(path: list[tuple[float, float]]) -> float:
    total = 0.0
    for start, end in _path_segments(path):
        total += _manhattan_distance(start, end)
    return total


def _dedupe_consecutive_points(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not points:
        return points
    out = [points[0]]
    for x, y in points[1:]:
        px, py = out[-1]
        if math.isclose(px, x, abs_tol=1e-6) and math.isclose(py, y, abs_tol=1e-6):
            continue
        out.append((x, y))
    return out


def _distance_sq(a: tuple[float, float], b: tuple[float, float]) -> float:
    dx = a[0] - b[0]
    dy = a[1] - b[1]
    return dx * dx + dy * dy


def _manhattan_distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _build_anchor_map(
    project: Project,
    refdes_map: dict[str, str],
    placement_map: dict[str, tuple[float, float]],
    source_format: SourceFormat,
    package_lookup: dict[str, Any],
) -> dict[str, dict[str, _ResolvedPinAnchor]]:
    symbol_lookup = {symbol.symbol_id: symbol for symbol in project.symbols}
    anchors: dict[str, dict[str, _ResolvedPinAnchor]] = {}
    for component in project.components:
        symbol_id = str(component.symbol_id or "").strip()
        if not symbol_id:
            continue
        symbol = symbol_lookup.get(symbol_id)
        if symbol is None or not symbol.pins:
            continue
        ref = _resolve_component_refdes(component, refdes_map)
        x0, y0 = placement_map.get(ref, (component.at.x_mm, component.at.y_mm))
        rotation_deg = _snapped_schematic_rotation_deg(
            _schematic_component_rotation_deg(
                component=component,
                source_format=source_format,
                package=_resolve_component_package_for_rotation(component, package_lookup),
            )
        )
        origin_x_mm, origin_y_mm = _symbol_origin_mm(symbol)
        local_points: list[tuple[float, float]] = []
        for pin in symbol.pins:
            key = str(pin.pin_number).strip()
            if not key:
                continue
            raw_x = float(pin.at.x_mm if pin.at else 0.0)
            raw_y = float(pin.at.y_mm if pin.at else 0.0)
            local_points.append((raw_x - origin_x_mm, raw_y - origin_y_mm))
        if local_points:
            local_center_x = sum(point[0] for point in local_points) / float(len(local_points))
            local_center_y = sum(point[1] for point in local_points) / float(len(local_points))
        else:
            local_center_x = 0.0
            local_center_y = 0.0
        pin_map: dict[str, _ResolvedPinAnchor] = {}
        for pin in symbol.pins:
            key = str(pin.pin_number).strip()
            if not key:
                continue
            raw_x = float(pin.at.x_mm if pin.at else 0.0)
            raw_y = float(pin.at.y_mm if pin.at else 0.0)
            local_x = raw_x - origin_x_mm
            local_y = raw_y - origin_y_mm
            dx, dy = _rotate_schematic_offset(local_x, local_y, rotation_deg)
            px = x0 + dx
            py = y0 + dy
            raw_pin_rotation = getattr(pin, "rotation_deg", None)
            outward_local = _pin_outward_local_vector(
                rotation_deg=raw_pin_rotation,
                local_x_mm=local_x - local_center_x,
                local_y_mm=local_y - local_center_y,
            )
            outward_world = _rotate_axis_vector(outward_local[0], outward_local[1], rotation_deg)
            pin_map[key] = _ResolvedPinAnchor(
                x_mm=px,
                y_mm=py,
                outward_dx=outward_world[0],
                outward_dy=outward_world[1],
            )
        if pin_map:
            anchors[ref] = pin_map
    return anchors


def _symbol_origin_mm(symbol: Any) -> tuple[float, float]:
    for graphic in getattr(symbol, "graphics", []) or []:
        if not isinstance(graphic, dict):
            continue
        if str(graphic.get("kind", "")).strip().lower() != "origin":
            continue
        try:
            return float(graphic.get("x_mm", 0.0)), float(graphic.get("y_mm", 0.0))
        except Exception:
            return 0.0, 0.0
    return 0.0, 0.0


def _build_external_anchor_map(
    project: Project,
    refdes_map: dict[str, str],
    placement_map: dict[str, tuple[float, float]],
    library_paths: dict[str, str],
    source_format: SourceFormat,
    package_lookup: dict[str, Any],
) -> dict[str, dict[str, _ResolvedPinAnchor]]:
    anchors: dict[str, dict[str, _ResolvedPinAnchor]] = {}
    cache: dict[tuple[str, str], dict[str, _CanonicalPinLocal]] = {}

    for component in project.components:
        device_id = str(component.device_id or "").strip()
        if not device_id:
            continue
        if ":" not in device_id:
            continue

        lib_name, device_name = device_id.split(":", 1)
        lib_name = lib_name.strip()
        device_name = device_name.strip()
        if not lib_name or not device_name:
            continue

        lib_ref = _resolve_library_ref(lib_name, library_paths)
        lib_path = Path(lib_ref.replace("\\", "/"))
        if not lib_path.exists():
            continue

        cache_key = (str(lib_path), device_name)
        if cache_key not in cache:
            cache[cache_key] = _external_device_pin_offsets(lib_path, device_name)
        pin_offsets = cache.get(cache_key, {})
        if not pin_offsets:
            continue

        ref = _resolve_component_refdes(component, refdes_map)
        x0, y0 = placement_map.get(ref, (component.at.x_mm, component.at.y_mm))
        rotation_deg = _snapped_schematic_rotation_deg(
            _schematic_component_rotation_deg(
                component=component,
                source_format=source_format,
                package=_resolve_component_package_for_rotation(component, package_lookup),
            )
        )
        transformed_pin_offsets: dict[str, _ResolvedPinAnchor] = {}
        for pin, pin_local in pin_offsets.items():
            tx, ty = _rotate_schematic_offset(pin_local.x_mm, pin_local.y_mm, rotation_deg)
            outward_world = _rotate_axis_vector(
                pin_local.outward_dx,
                pin_local.outward_dy,
                rotation_deg,
            )
            transformed_pin_offsets[pin] = _ResolvedPinAnchor(
                x_mm=x0 + tx,
                y_mm=y0 + ty,
                outward_dx=outward_world[0],
                outward_dy=outward_world[1],
            )
        anchors[ref] = transformed_pin_offsets

    return anchors


def _build_external_local_pin_map(
    project: Project,
    refdes_map: dict[str, str],
    library_paths: dict[str, str],
) -> dict[str, dict[str, _CanonicalPinLocal]]:
    out: dict[str, dict[str, _CanonicalPinLocal]] = {}
    cache: dict[tuple[str, str], dict[str, _CanonicalPinLocal]] = {}
    for component in project.components:
        ref = _resolve_component_refdes(component, refdes_map)
        offsets = _component_external_pin_offsets(
            component=component,
            library_paths=library_paths,
            cache=cache,
        )
        if offsets:
            out[ref] = offsets
    return out


def _external_device_pin_offsets(lib_path: Path, device_name: str) -> dict[str, _CanonicalPinLocal]:
    root = _parse_library_root(lib_path)
    if root is None:
        return {}

    lib = root.find(".//library")
    if lib is None:
        return {}

    symbol_pin_maps = _symbol_pin_maps(lib)
    for deviceset in lib.findall("./devicesets/deviceset"):
        ds_name = str(deviceset.get("name") or "").strip()
        if not ds_name:
            continue

        gate_defs: dict[str, tuple[str, float, float, float, bool]] = {}
        gates = deviceset.find("./gates")
        if gates is not None:
            for gate in gates.findall("./gate"):
                gate_name = str(gate.get("name") or "").strip()
                symbol_name = str(gate.get("symbol") or "").strip()
                if not gate_name or not symbol_name:
                    continue
                gate_x_mm = _coord_to_mm(_safe_float(gate.get("x")))
                gate_y_mm = _coord_to_mm(_safe_float(gate.get("y")))
                gate_rotation_deg, gate_mirrored = _gate_rotation_and_mirror(str(gate.get("rot") or ""))
                gate_defs[gate_name] = (
                    symbol_name,
                    gate_x_mm,
                    gate_y_mm,
                    gate_rotation_deg,
                    gate_mirrored,
                )

        devices = deviceset.find("./devices")
        if devices is None:
            continue

        for device in devices.findall("./device"):
            variant = str(device.get("name") or "").strip()
            full_name = f"{ds_name}{variant}" if variant else ds_name
            if _norm_token(full_name) != _norm_token(device_name):
                continue

            connects = device.find("./connects")
            if connects is None:
                return {}

            offsets: dict[str, _CanonicalPinLocal] = {}
            for connect in connects.findall("./connect"):
                gate_name = str(connect.get("gate") or "").strip()
                symbol_pin_name = str(connect.get("pin") or "").strip()
                pad_name = str(connect.get("pad") or "").strip()
                if not gate_name or not symbol_pin_name or not pad_name:
                    continue
                gate = gate_defs.get(gate_name)
                if gate is None:
                    continue

                symbol_name, gate_x_mm, gate_y_mm, gate_rotation_deg, gate_mirrored = gate
                pin_map = symbol_pin_maps.get(symbol_name, {})
                pin_offset = pin_map.get(symbol_pin_name)
                if pin_offset is None:
                    continue

                pin_x_mm = pin_offset.x_mm
                pin_y_mm = pin_offset.y_mm
                outward_x = pin_offset.outward_dx
                outward_y = pin_offset.outward_dy
                if gate_mirrored:
                    pin_x_mm = -pin_x_mm
                    outward_x = -outward_x
                rot_x_mm, rot_y_mm = _rotate_schematic_offset(pin_x_mm, pin_y_mm, gate_rotation_deg)
                outward_rot_x, outward_rot_y = _rotate_axis_vector(outward_x, outward_y, gate_rotation_deg)
                offsets[pad_name] = _CanonicalPinLocal(
                    x_mm=gate_x_mm + rot_x_mm,
                    y_mm=gate_y_mm + rot_y_mm,
                    outward_dx=outward_rot_x,
                    outward_dy=outward_rot_y,
                )

            return offsets

    return {}


def _symbol_pin_maps(library_el: ET.Element) -> dict[str, dict[str, _CanonicalPinLocal]]:
    out: dict[str, dict[str, _CanonicalPinLocal]] = {}
    symbols = library_el.find("./symbols")
    if symbols is None:
        return out

    for symbol in symbols.findall("./symbol"):
        symbol_name = str(symbol.get("name") or "").strip()
        if not symbol_name:
            continue
        symbol_bounds = _symbol_graphic_bounds(symbol)
        pin_map: dict[str, _CanonicalPinLocal] = {}
        for pin in symbol.findall("./pin"):
            pin_name = str(pin.get("name") or "").strip()
            if not pin_name:
                continue
            x_mm = _coord_to_mm(_safe_float(pin.get("x")))
            y_mm = _coord_to_mm(_safe_float(pin.get("y")))
            pin_rotation_deg, pin_mirrored = _gate_rotation_and_mirror(str(pin.get("rot") or "R0"))
            inward_dx, inward_dy = _direction_from_rotation(pin_rotation_deg)
            if pin_mirrored:
                inward_dx = -inward_dx
            outward_dx = -inward_dx
            outward_dy = -inward_dy
            pin_len_mm = _pin_length_mm(pin)
            if _should_shift_pin_endpoint_by_length(
                x_mm=x_mm,
                y_mm=y_mm,
                outward_dx=outward_dx,
                outward_dy=outward_dy,
                pin_length_mm=pin_len_mm,
                symbol_bounds=symbol_bounds,
            ):
                x_mm += outward_dx * pin_len_mm
                y_mm += outward_dy * pin_len_mm
            pin_map[pin_name] = _CanonicalPinLocal(
                x_mm=x_mm,
                y_mm=y_mm,
                outward_dx=outward_dx,
                outward_dy=outward_dy,
            )
        if pin_map:
            out[symbol_name] = pin_map
    return out


def _pin_length_mm(pin_el: ET.Element) -> float:
    token = str(pin_el.get("length") or "").strip().lower()
    if not token:
        return 0.0
    named = {
        "point": 0.0,
        "short": 2.54,
        "middle": 5.08,
        "long": 7.62,
    }
    if token in named:
        return named[token]
    numeric = _safe_float(token)
    if numeric <= 0.0:
        return 0.0
    return _coord_to_mm(numeric)


def _symbol_graphic_bounds(symbol_el: ET.Element) -> tuple[float, float, float, float] | None:
    xs: list[float] = []
    ys: list[float] = []

    def _append_xy(x_raw: object, y_raw: object) -> None:
        xs.append(_coord_to_mm(_safe_float(x_raw)))
        ys.append(_coord_to_mm(_safe_float(y_raw)))

    for wire in symbol_el.findall("./wire"):
        _append_xy(wire.get("x1"), wire.get("y1"))
        _append_xy(wire.get("x2"), wire.get("y2"))

    for rect in symbol_el.findall("./rectangle"):
        _append_xy(rect.get("x1"), rect.get("y1"))
        _append_xy(rect.get("x2"), rect.get("y2"))

    for circle in symbol_el.findall("./circle"):
        cx = _coord_to_mm(_safe_float(circle.get("x")))
        cy = _coord_to_mm(_safe_float(circle.get("y")))
        r = abs(_coord_to_mm(_safe_float(circle.get("radius"))))
        xs.extend([cx - r, cx + r])
        ys.extend([cy - r, cy + r])

    for polygon in symbol_el.findall("./polygon"):
        for vertex in polygon.findall("./vertex"):
            _append_xy(vertex.get("x"), vertex.get("y"))

    if not xs or not ys:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _should_shift_pin_endpoint_by_length(
    x_mm: float,
    y_mm: float,
    outward_dx: float,
    outward_dy: float,
    pin_length_mm: float,
    symbol_bounds: tuple[float, float, float, float] | None,
) -> bool:
    if symbol_bounds is None:
        return False
    if pin_length_mm <= 0.0:
        return False
    margin_mm = 0.25
    if not _point_in_bounds_with_margin(x_mm, y_mm, symbol_bounds, margin_mm):
        return False
    shifted_x = x_mm + outward_dx * pin_length_mm
    shifted_y = y_mm + outward_dy * pin_length_mm
    return not _point_in_bounds_with_margin(shifted_x, shifted_y, symbol_bounds, margin_mm)


def _point_in_bounds_with_margin(
    x_mm: float,
    y_mm: float,
    bounds: tuple[float, float, float, float],
    margin_mm: float,
) -> bool:
    min_x, min_y, max_x, max_y = bounds
    return (
        (float(min_x) - margin_mm) <= float(x_mm) <= (float(max_x) + margin_mm)
        and (float(min_y) - margin_mm) <= float(y_mm) <= (float(max_y) + margin_mm)
    )


def _safe_float(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value or "0"))
    except Exception:
        return 0.0


def _coord_to_mm(value: float) -> float:
    # EAGLE/Fusion library XML stores symbol/package coordinates in metric-style values.
    # Preserve coordinates directly for anchor alignment with scripted ADD placement.
    return float(value)


def _norm_token(value: str) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def _snapped_schematic_rotation_deg(rotation_deg: float) -> int:
    try:
        angle = float(rotation_deg)
    except Exception:
        angle = 0.0
    snapped = int(round(angle / 90.0)) * 90
    return snapped % 360


def _schematic_rotation_token(rotation_deg: float) -> str:
    return f"R{_snapped_schematic_rotation_deg(rotation_deg)}"


def _schematic_component_rotation_deg(
    component: Any,
    source_format: SourceFormat,
    package: Any | None = None,
) -> float:
    raw_rotation = float(getattr(component, "rotation_deg", 0.0) or 0.0)
    # EasyEDA Pro schematic symbol rotation sign is opposite to Fusion/EAGLE
    # ADD token rotation sign; flipping here keeps routed anchors attached in
    # Fusion without changing internal anchor-routing math.
    if source_format == SourceFormat.EASYEDA_PRO:
        raw_rotation = -raw_rotation
        if _component_is_resistor(component):
            snapped = _snapped_schematic_rotation_deg(raw_rotation)
            if package is not None and _package_pin_count(package) == 2:
                snapped = int(_canonicalize_two_pin_quarter_turn(snapped))
            if package is not None and _is_adjustable_resistor_package(package) and snapped == 180:
                snapped = 90
            return float(snapped)
    return raw_rotation


def _schematic_component_rotation_token(
    component: Any,
    source_format: SourceFormat,
    package: Any | None = None,
) -> str:
    return _schematic_rotation_token(
        _schematic_component_rotation_deg(component, source_format, package=package)
    )


def _rotate_schematic_offset(x_mm: float, y_mm: float, rotation_deg: float) -> tuple[float, float]:
    angle = float(rotation_deg) % 360.0
    if math.isclose(angle, 0.0, abs_tol=1e-9):
        return (x_mm, y_mm)
    if math.isclose(angle, 90.0, abs_tol=1e-9):
        return (-y_mm, x_mm)
    if math.isclose(angle, 180.0, abs_tol=1e-9):
        return (-x_mm, -y_mm)
    if math.isclose(angle, 270.0, abs_tol=1e-9):
        return (y_mm, -x_mm)
    radians = math.radians(angle)
    cos_a = math.cos(radians)
    sin_a = math.sin(radians)
    return (
        x_mm * cos_a - y_mm * sin_a,
        x_mm * sin_a + y_mm * cos_a,
    )


def _direction_from_rotation(rotation_deg: float | int) -> tuple[float, float]:
    angle = _snapped_schematic_rotation_deg(float(rotation_deg))
    if angle == 0:
        return (1.0, 0.0)
    if angle == 90:
        return (0.0, 1.0)
    if angle == 180:
        return (-1.0, 0.0)
    if angle == 270:
        return (0.0, -1.0)
    return (1.0, 0.0)


def _normalize_axis_direction(dx: float, dy: float) -> tuple[float, float]:
    fx = float(dx)
    fy = float(dy)
    if abs(fx) < 1e-9 and abs(fy) < 1e-9:
        return (0.0, 0.0)
    if abs(fx) >= abs(fy):
        return (1.0 if fx > 0.0 else -1.0, 0.0)
    return (0.0, 1.0 if fy > 0.0 else -1.0)


def _direction_from_offset(dx_mm: float, dy_mm: float) -> tuple[float, float]:
    if abs(float(dx_mm)) < 1e-9 and abs(float(dy_mm)) < 1e-9:
        return (-1.0, 0.0)
    return _normalize_axis_direction(float(dx_mm), float(dy_mm))


def _rotate_axis_vector(dx: float, dy: float, rotation_deg: float) -> tuple[float, float]:
    rx, ry = _rotate_schematic_offset(float(dx), float(dy), float(rotation_deg))
    return _normalize_axis_direction(rx, ry)


def _pin_outward_local_vector(
    rotation_deg: float | None,
    local_x_mm: float,
    local_y_mm: float,
) -> tuple[float, float]:
    if rotation_deg is not None:
        inward_dx, inward_dy = _direction_from_rotation(rotation_deg)
        # Pin rotation usually points from connection anchor into the symbol.
        # Outward wire exit direction is the opposite.
        outward = (-inward_dx, -inward_dy)
        if outward != (0.0, 0.0):
            return outward
    return _direction_from_offset(local_x_mm, local_y_mm)


def _fallback_anchor_from_component_center(
    placement_map: dict[str, tuple[float, float]],
    refdes: str,
    anchor_x_mm: float,
    anchor_y_mm: float,
) -> _ResolvedPinAnchor:
    cx, cy = placement_map.get(refdes, (anchor_x_mm, anchor_y_mm))
    outward_dx, outward_dy = _direction_from_offset(anchor_x_mm - cx, anchor_y_mm - cy)
    return _ResolvedPinAnchor(
        x_mm=float(anchor_x_mm),
        y_mm=float(anchor_y_mm),
        outward_dx=outward_dx,
        outward_dy=outward_dy,
    )


def _gate_rotation_and_mirror(rotation_attr: str) -> tuple[float, bool]:
    token = str(rotation_attr or "").strip().upper()
    if not token:
        return 0.0, False

    normalized = token.replace("S", "")
    mirrored = normalized.startswith("M")
    match = re.search(r"R(-?\d+(?:\.\d+)?)", normalized)
    if match is None:
        match = re.search(r"(-?\d+(?:\.\d+)?)", normalized)
    if match is None:
        return 0.0, mirrored
    try:
        angle = float(match.group(1))
    except Exception:
        angle = 0.0
    return angle, mirrored


def _parse_library_root(lib_path: Path) -> ET.Element | None:
    return parse_xml_root_with_entity_sanitization(lib_path)


def _normalize_power_net_name(name: str) -> str | None:
    token = _norm_token(name)
    if not token:
        return None

    direct_map = {
        "GND": "GND",
        "AGND": "AGND",
        "DGND": "DGND",
        "PGND": "PGND",
        "EARTH": "EARTH",
        "CHASSIS": "CHASSIS",
        "VCC": "VCC",
        "VDD": "VDD",
        "VSS": "VSS",
        "VBAT": "VBAT",
        "VIN": "VIN",
        "AVDD": "AVDD",
        "DVDD": "DVDD",
    }
    if token in direct_map:
        return direct_map[token]

    voltage_patterns = (
        (r"^3V?3$", "3V3"),
        (r"^33V$", "3V3"),
        (r"^V33$", "3V3"),
        (r"^5V0?$", "5V"),
        (r"^V5$", "5V"),
        (r"^12V0?$", "12V"),
        (r"^V12$", "12V"),
    )
    for pattern, normalized in voltage_patterns:
        if re.fullmatch(pattern, token):
            return normalized
    return None


def _allocate_supply_ref(used_refs: set[str], start_idx: int) -> tuple[str, int]:
    idx = max(1, int(start_idx))
    while True:
        ref = f"PWRSYM_{idx}"
        if ref not in used_refs:
            return ref, idx + 1
        idx += 1


def _supply_symbol_position(
    mapped_nodes: list[tuple[str, str, float, float]],
    ordinal: int,
) -> tuple[float, float]:
    if not mapped_nodes:
        return 10.0 + ordinal * 2.0, 10.0
    xs = [float(x) for _, _, x, _ in mapped_nodes]
    ys = [float(y) for _, _, _, y in mapped_nodes]
    x = min(xs) - 8.0 - (ordinal % 5) * 1.2
    y = max(ys) + 2.0 + (ordinal % 3) * 1.0
    return x, y
