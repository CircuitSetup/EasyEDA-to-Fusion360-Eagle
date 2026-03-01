from __future__ import annotations

from pathlib import Path
import math
import re
from collections import defaultdict
from typing import Iterable
import xml.etree.ElementTree as ET

from easyeda2fusion.model import Net, NetNode, Project
from easyeda2fusion.builders.board_reconstruction import _build_track_net_aliases


class SchematicReconstructionBuilder:
    """Builds EAGLE script command lines for schematic reconstruction."""

    def build_commands(
        self,
        project: Project,
        library_paths: dict[str, str] | None = None,
    ) -> list[str]:
        lines: list[str] = [
            "GRID MM 0.1 ON;",
            "SET WIRE_BEND 2;",
        ]

        net_alias: dict[str, str] = {}
        if project.board is not None:
            net_alias = _build_track_net_aliases(project.board.tracks, project.board.vias)
        effective_nets = _coalesced_nets(project, net_alias)

        refdes_map = _build_refdes_map(project)
        placement_map = _auto_layout_positions(project, refdes_map)
        placed_refs: set[str] = set()
        valid_pins_by_ref = _valid_pins_by_ref(project)
        component_by_safe_ref = {
            _resolve_component_refdes(component, refdes_map): component
            for component in project.components
        }
        resolved_library_paths = _normalized_library_paths(library_paths or {})
        anchor_map = _build_anchor_map(project, refdes_map, placement_map)
        external_anchor_map = _build_external_anchor_map(
            project=project,
            refdes_map=refdes_map,
            placement_map=placement_map,
            library_paths=resolved_library_paths,
        )
        for refdes, pin_map in external_anchor_map.items():
            anchor_map.setdefault(refdes, {}).update(pin_map)

        # Optional cleanup for reruns can be enabled via metadata flag, but keep
        # disabled by default to avoid noisy "Invalid part" errors on clean imports.
        if bool(project.metadata.get("schematic_delete_existing_parts")):
            for component in project.components:
                safe_refdes = _resolve_component_refdes(component, refdes_map)
                lines.append(f"DELETE {safe_refdes};")

        for component in project.components:
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
                lines.append(
                    f"ADD {_quote_token(script_device_token)} {_add_part_name_token(safe_refdes)} ({at_x:.4f} {at_y:.4f}) R0;"
                )
                value_text = _component_value_for_schematic(component)
                if value_text:
                    lines.append(
                        f"VALUE {_add_part_name_token(safe_refdes)} {_quote_token(value_text)};"
                    )

        inserted_supply_symbols: list[dict[str, str]] = []
        pending_label_stubs: list[tuple[str, float, float, float, float]] = []
        supply_supported = False

        occupied_segments: list[tuple[str, tuple[float, float], tuple[float, float]]] = []
        for net in effective_nets:
            if not net.nodes:
                continue
            mapped_nodes: list[tuple[str, str, float, float]] = []
            seen_nodes: set[tuple[str, str]] = set()
            for node in net.nodes:
                ref = refdes_map.get(node.refdes, _sanitize_refdes(node.refdes))
                if ref not in placed_refs:
                    continue
                pin = str(node.pin).strip()
                if not pin:
                    continue
                valid_pins = valid_pins_by_ref.get(ref, set())
                if valid_pins and pin not in valid_pins:
                    continue
                key = (ref, pin)
                if key in seen_nodes:
                    continue
                seen_nodes.add(key)
                ref_anchors = anchor_map.get(ref, {})
                if pin in ref_anchors:
                    x, y = ref_anchors[pin]
                else:
                    # Fall back to deterministic synthetic anchors for any unresolved
                    # pin so multi-pin components are still connected.
                    x, y = _net_anchor(placement_map, anchor_map, ref, pin)
                mapped_nodes.append((ref, pin, x, y))

            power_key = _normalize_power_net_name(net.name)
            if power_key and mapped_nodes and supply_supported:
                pass

            if not _should_draw_net(net.name, mapped_nodes):
                continue

            net_paths = _route_net_paths(
                net.name,
                mapped_nodes,
                occupied_segments,
                placement_map=placement_map,
            )
            used_fallback_stubs = False
            if net_paths and _paths_need_label_fallback(net.name, net_paths, occupied_segments):
                fallback_paths = _stub_paths_for_net(
                    net_name=net.name,
                    mapped_nodes=mapped_nodes,
                    placement_map=placement_map,
                    occupied_segments=occupied_segments,
                )
                if fallback_paths:
                    net_paths = fallback_paths
                    used_fallback_stubs = True
            if not net_paths:
                fallback_paths = _stub_paths_for_net(
                    net_name=net.name,
                    mapped_nodes=mapped_nodes,
                    placement_map=placement_map,
                    occupied_segments=occupied_segments,
                )
                if fallback_paths:
                    net_paths = fallback_paths
                    used_fallback_stubs = True
            emitted_for_net = False
            deferred_fallback_paths: list[list[tuple[float, float]]] = []
            for path in net_paths:
                if len(path) < 2:
                    continue
                if used_fallback_stubs:
                    collision_score = _path_collision_score(path, net.name, occupied_segments)
                    if collision_score > 0:
                        deferred_fallback_paths.append(path)
                        if _fallback_net_should_emit_labels(
                            mapped_nodes,
                            component_by_safe_ref,
                        ):
                            label_spec = _label_spec_for_path(path)
                            if label_spec is not None:
                                pending_label_stubs.append((str(net.name), *label_spec))
                        continue
                coords = " ".join(f"({x:.4f} {y:.4f})" for x, y in path)
                lines.append(f"NET {_quote_token(net.name)} {coords};")
                _append_occupied_segments(occupied_segments, net.name, path)
                emitted_for_net = True
                if power_key:
                    label_spec = _label_spec_for_path(path)
                    if label_spec is not None:
                        pending_label_stubs.append((str(net.name), *label_spec))
                if used_fallback_stubs and _fallback_net_should_emit_labels(
                    mapped_nodes,
                    component_by_safe_ref,
                ):
                    label_spec = _label_spec_for_path(path)
                    if label_spec is not None:
                        pending_label_stubs.append((str(net.name), *label_spec))
            if used_fallback_stubs and not emitted_for_net and deferred_fallback_paths:
                # Keep at least one routed segment so labels have an electrical segment to attach to.
                best_path = min(
                    deferred_fallback_paths,
                    key=lambda item: (
                        _path_collision_score(item, net.name, occupied_segments),
                        _path_total_length(item),
                    ),
                )
                coords = " ".join(f"({x:.4f} {y:.4f})" for x, y in best_path)
                lines.append(f"NET {_quote_token(net.name)} {coords};")
                _append_occupied_segments(occupied_segments, net.name, best_path)

        for sheet in project.sheets:
            for note in sheet.annotations:
                text = note.text.replace("'", "")
                if text:
                    lines.append(
                        f"TEXT '{text}' ({note.at.x_mm:.4f} {note.at.y_mm:.4f});"
                    )

        if pending_label_stubs:
            lines.append("CHANGE XREF ON;")
            lines.append("CHANGE SIZE 1.27;")
            seen_points: set[tuple[float, float, float, float]] = set()
            for _net_name, pick_x, pick_y, label_x, label_y in pending_label_stubs:
                key = (round(pick_x, 4), round(pick_y, 4), round(label_x, 4), round(label_y, 4))
                if key in seen_points:
                    continue
                seen_points.add(key)
                # LABEL requires a pick point on a net segment and a placement point.
                lines.append(
                    f"LABEL ({pick_x:.4f} {pick_y:.4f}) ({label_x:.4f} {label_y:.4f});"
                )

        lines = _orthogonalize_schematic_orientations(lines)
        project.metadata["supply_symbols_inserted"] = inserted_supply_symbols
        project.metadata["supply_symbols_mode"] = "disabled"
        return lines


def _build_refdes_map(project: Project) -> dict[str, str]:
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for ordinal, component in enumerate(project.components, start=1):
        original = component.refdes
        base = _sanitize_refdes(original)
        candidate = base
        suffix_idx = 2
        while candidate in used:
            candidate = f"{base}_{suffix_idx}"
            suffix_idx += 1
        mapping.setdefault(original, candidate)
        mapping[_component_refdes_key(component, ordinal)] = candidate
        used.add(candidate)
    return mapping


def _coalesced_nets(project: Project, net_alias: dict[str, str]) -> list[Net]:
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
            ref = str(getattr(node, "refdes", "") or "").strip()
            pin = str(getattr(node, "pin", "") or "").strip()
            if not ref or not pin:
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
    text = str(value or "").strip()
    text = re.sub(r"[^A-Za-z0-9_]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    if not text:
        return "U_AUTO"
    if not text[0].isalpha():
        text = f"U_{text}"
    return text


def _add_part_name_token(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return text
    # Quote all part names in ADD commands to avoid ambiguity with command tokens.
    return _quote_token(text)


def _component_refdes_key(component, ordinal: int) -> str:
    source_id = str(getattr(component, "source_instance_id", "") or "").strip()
    if source_id:
        return f"{component.refdes}::{source_id}"
    return f"{component.refdes}::IDX{ordinal}"


def _resolve_component_refdes(component, refdes_map: dict[str, str]) -> str:
    source_id = str(getattr(component, "source_instance_id", "") or "").strip()
    if source_id:
        keyed = refdes_map.get(f"{component.refdes}::{source_id}")
        if keyed:
            return keyed
    return refdes_map.get(component.refdes, _sanitize_refdes(component.refdes))


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


def _orthogonalize_schematic_orientations(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("ADD "):
            out.append(_snap_orientation_suffix(line))
            continue
        if stripped.startswith("ROTATE "):
            out.append(_snap_orientation_suffix(line))
            continue
        if stripped.startswith("TEXT "):
            out.append(_snap_orientation_suffix(line))
            continue
        out.append(line)
    return out


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


def _auto_layout_positions(project: Project, refdes_map: dict[str, str]) -> dict[str, tuple[float, float]]:
    components = list(project.components)
    if not components:
        return {}

    placed = _board_like_positions(components, refdes_map)
    if placed is not None:
        return placed

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
    package_lookup: dict[str, set[str]] = {}
    for package in project.packages:
        pins = {
            str(pad.pad_number).strip()
            for pad in package.pads
            if str(pad.pad_number).strip()
        }
        package_lookup[package.package_id] = pins
        package_lookup[package.name] = pins

    valid: dict[str, set[str]] = {}
    for component in project.components:
        ref = _sanitize_refdes(component.refdes)
        package_id = str(component.package_id or "").strip()
        if not package_id:
            valid[ref] = set()
            continue
        valid[ref] = set(package_lookup.get(package_id, set()))
    return valid


def _net_anchor(
    placement_map: dict[str, tuple[float, float]],
    anchor_map: dict[str, dict[str, tuple[float, float]]],
    refdes: str,
    pin: str,
) -> tuple[float, float]:
    pin_key = str(pin or "").strip()
    if refdes in anchor_map and pin_key in anchor_map[refdes]:
        return anchor_map[refdes][pin_key]

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
) -> bool:
    for ref, other in placement_map.items():
        if ref == occupied_ref:
            continue
        if math.hypot(point[0] - other[0], point[1] - other[1]) < min_clearance_mm:
            return False
    return True


def _component_is_rc_passive(component) -> bool:
    if component is None:
        return False
    ref = _sanitize_refdes(getattr(component, "refdes", "")).upper()
    return ref.startswith("R") or ref.startswith("C")


def _paths_need_label_fallback(
    net_name: str,
    net_paths: list[list[tuple[float, float]]],
    occupied_segments: list[tuple[str, tuple[float, float], tuple[float, float]]],
) -> bool:
    if not net_paths:
        return True
    quality = _net_path_quality(net_paths, occupied_segments)
    if quality[0] > 0:
        return True
    collision_score = sum(
        _path_collision_score(path, net_name, occupied_segments)
        for path in net_paths
    )
    return collision_score > 0


def _fallback_net_should_emit_labels(
    mapped_nodes: list[tuple[str, str, float, float]],
    component_by_safe_ref: dict[str, object],
) -> bool:
    refs = {str(ref).strip() for ref, _, _, _ in mapped_nodes if str(ref).strip()}
    if not refs:
        return False
    return True


def _stub_label_point(path: list[tuple[float, float]]) -> tuple[float, float] | None:
    if len(path) < 2:
        return None
    return path[-1]


def _label_spec_for_path(path: list[tuple[float, float]]) -> tuple[float, float, float, float] | None:
    segments = _path_segments(path)
    if not segments:
        return None

    start, end = segments[0]
    pick_x = (start[0] + end[0]) / 2.0
    pick_y = (start[1] + end[1]) / 2.0

    dx = end[0] - start[0]
    dy = end[1] - start[1]
    if abs(dx) >= abs(dy):
        label_x = end[0]
        label_y = end[1] + (1.27 if dy >= 0 else -1.27)
    else:
        label_x = end[0] + (1.27 if dx >= 0 else -1.27)
        label_y = end[1]

    return (pick_x, pick_y, label_x, label_y)


def _is_ground_net_name(name: str) -> bool:
    token = "".join(ch for ch in str(name or "").upper() if ch.isalnum())
    return token in {"GND", "AGND", "DGND", "PGND", "SGND", "VSS", "VSSA", "VSSD"}


def _route_net_paths(
    net_name: str,
    mapped_nodes: list[tuple[str, str, float, float]],
    occupied_segments: list[tuple[str, tuple[float, float], tuple[float, float]]],
    placement_map: dict[str, tuple[float, float]] | None = None,
) -> list[list[tuple[float, float]]]:
    points = _unique_points_from_nodes(mapped_nodes)
    if len(points) < 2:
        return []

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
        for start, end in edges:
            path = _route_path_between_points(
                start=start,
                end=end,
                net_name=net_name,
                occupied_segments=[*occupied_segments, *local_segments],
            )
            if len(path) < 2:
                continue
            paths.append(path)
            _append_occupied_segments(local_segments, net_name, path)

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
    for start, end in segments:
        for _, occ_start, occ_end in occupied_segments:
            if _segments_share_endpoint(start, end, occ_start, occ_end):
                continue
            if _axis_segments_touch(start, end, occ_start, occ_end):
                external_touches += 1

    return (internal_intersections, external_touches, total_length)


def _stub_paths_for_net(
    net_name: str,
    mapped_nodes: list[tuple[str, str, float, float]],
    placement_map: dict[str, tuple[float, float]],
    occupied_segments: list[tuple[str, tuple[float, float], tuple[float, float]]],
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
        for path in candidates:
            score = _path_collision_score(
                path,
                net_name,
                [*occupied_segments, *local_occupied],
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


def _unique_points_from_nodes(mapped_nodes: list[tuple[str, str, float, float]]) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    seen: set[tuple[float, float]] = set()
    for _, _, x, y in mapped_nodes:
        key = (round(x, 4), round(y, 4))
        if key in seen:
            continue
        seen.add(key)
        points.append((x, y))
    return points


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


def _route_path_between_points(
    start: tuple[float, float],
    end: tuple[float, float],
    net_name: str,
    occupied_segments: list[tuple[str, tuple[float, float], tuple[float, float]]],
) -> list[tuple[float, float]]:
    candidates = _manhattan_path_candidates(start, end)
    if not candidates:
        return []

    ranked = sorted(
        candidates,
        key=lambda path: (
            _path_collision_score(path, net_name, occupied_segments),
            _path_total_length(path),
        ),
    )
    return ranked[0]


def _manhattan_path_candidates(
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


def _is_orthogonal_path(path: list[tuple[float, float]]) -> bool:
    if len(path) < 2:
        return False
    for idx in range(len(path) - 1):
        x1, y1 = path[idx]
        x2, y2 = path[idx + 1]
        if not (math.isclose(x1, x2, abs_tol=1e-6) or math.isclose(y1, y2, abs_tol=1e-6)):
            return False
    return True


def _path_collision_score(
    path: list[tuple[float, float]],
    net_name: str,
    occupied_segments: list[tuple[str, tuple[float, float], tuple[float, float]]],
) -> int:
    score = 0
    path_segments = _path_segments(path)
    for left in path_segments:
        for occ_net, occ_start, occ_end in occupied_segments:
            if _axis_segments_touch(left[0], left[1], occ_start, occ_end):
                if occ_net == net_name:
                    if _segments_share_endpoint(left[0], left[1], occ_start, occ_end):
                        continue
                    # Same-net overlap still triggers interactive merge prompts in Fusion/EAGLE.
                    score += 600
                else:
                    score += 400
    return score


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
    a_vertical = math.isclose(ax1, ax2, abs_tol=eps)
    b_vertical = math.isclose(bx1, bx2, abs_tol=eps)

    if a_vertical and b_vertical:
        if not math.isclose(ax1, bx1, abs_tol=eps):
            return False
        a0, a1 = sorted((ay1, ay2))
        b0, b1 = sorted((by1, by2))
        return max(a0, b0) <= min(a1, b1) + eps

    if not a_vertical and not b_vertical:
        if not math.isclose(ay1, by1, abs_tol=eps):
            return False
        a0, a1 = sorted((ax1, ax2))
        b0, b1 = sorted((bx1, bx2))
        return max(a0, b0) <= min(a1, b1) + eps

    if a_vertical:
        x = ax1
        y = by1
        bx0, bx1s = sorted((bx1, bx2))
        ay0, ay1s = sorted((ay1, ay2))
        return bx0 - eps <= x <= bx1s + eps and ay0 - eps <= y <= ay1s + eps

    x = bx1
    y = ay1
    ax0, ax1s = sorted((ax1, ax2))
    by0, by1s = sorted((by1, by2))
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
) -> dict[str, dict[str, tuple[float, float]]]:
    symbol_lookup = {symbol.symbol_id: symbol for symbol in project.symbols}
    anchors: dict[str, dict[str, tuple[float, float]]] = {}
    for component in project.components:
        symbol_id = str(component.symbol_id or "").strip()
        if not symbol_id:
            continue
        symbol = symbol_lookup.get(symbol_id)
        if symbol is None or not symbol.pins:
            continue
        ref = _resolve_component_refdes(component, refdes_map)
        x0, y0 = placement_map.get(ref, (component.at.x_mm, component.at.y_mm))
        pin_map: dict[str, tuple[float, float]] = {}
        for pin in symbol.pins:
            key = str(pin.pin_number).strip()
            if not key:
                continue
            px = x0 + float(pin.at.x_mm if pin.at else 0.0)
            py = y0 + float(pin.at.y_mm if pin.at else 0.0)
            pin_map[key] = (px, py)
        if pin_map:
            anchors[ref] = pin_map
    return anchors


def _build_external_anchor_map(
    project: Project,
    refdes_map: dict[str, str],
    placement_map: dict[str, tuple[float, float]],
    library_paths: dict[str, str],
) -> dict[str, dict[str, tuple[float, float]]]:
    anchors: dict[str, dict[str, tuple[float, float]]] = {}
    cache: dict[tuple[str, str], dict[str, tuple[float, float]]] = {}

    for component in project.components:
        device_id = str(component.device_id or "").strip()
        if not device_id or device_id.startswith("easyeda_generated:"):
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
        anchors[ref] = {
            pin: (x0 + dx, y0 + dy)
            for pin, (dx, dy) in pin_offsets.items()
        }

    return anchors


def _external_device_pin_offsets(lib_path: Path, device_name: str) -> dict[str, tuple[float, float]]:
    try:
        root = ET.parse(lib_path).getroot()
    except Exception:
        return {}

    lib = root.find(".//library")
    if lib is None:
        return {}

    symbol_pin_maps = _symbol_pin_maps(lib)
    for deviceset in lib.findall("./devicesets/deviceset"):
        ds_name = str(deviceset.get("name") or "").strip()
        if not ds_name:
            continue

        gate_defs: dict[str, tuple[str, float, float]] = {}
        gates = deviceset.find("./gates")
        if gates is not None:
            for gate in gates.findall("./gate"):
                gate_name = str(gate.get("name") or "").strip()
                symbol_name = str(gate.get("symbol") or "").strip()
                if not gate_name or not symbol_name:
                    continue
                gate_defs[gate_name] = (
                    symbol_name,
                    0.0,
                    0.0,
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

            offsets: dict[str, tuple[float, float]] = {}
            for connect in connects.findall("./connect"):
                gate_name = str(connect.get("gate") or "").strip()
                symbol_pin_name = str(connect.get("pin") or "").strip()
                pad_name = str(connect.get("pad") or "").strip()
                if not gate_name or not symbol_pin_name or not pad_name:
                    continue
                gate = gate_defs.get(gate_name)
                if gate is None:
                    continue

                symbol_name, gate_x_mm, gate_y_mm = gate
                pin_map = symbol_pin_maps.get(symbol_name, {})
                pin_offset = pin_map.get(symbol_pin_name)
                if pin_offset is None:
                    continue

                offsets[pad_name] = (gate_x_mm + pin_offset[0], gate_y_mm + pin_offset[1])

            return offsets

    return {}


def _symbol_pin_maps(library_el: ET.Element) -> dict[str, dict[str, tuple[float, float]]]:
    out: dict[str, dict[str, tuple[float, float]]] = {}
    symbols = library_el.find("./symbols")
    if symbols is None:
        return out

    for symbol in symbols.findall("./symbol"):
        symbol_name = str(symbol.get("name") or "").strip()
        if not symbol_name:
            continue
        pin_map: dict[str, tuple[float, float]] = {}
        for pin in symbol.findall("./pin"):
            pin_name = str(pin.get("name") or "").strip()
            if not pin_name:
                continue
            x_mm = _coord_to_mm(_safe_float(pin.get("x")))
            y_mm = _coord_to_mm(_safe_float(pin.get("y")))
            pin_map[pin_name] = (x_mm, y_mm)
        if pin_map:
            out[symbol_name] = pin_map
    return out


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
