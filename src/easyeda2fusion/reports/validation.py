from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from easyeda2fusion.builders.component_identity import component_instance_key
from easyeda2fusion.builders.schematic_inference import SchematicInferenceReport
from easyeda2fusion.matchers.library_matcher import MatchContext
from easyeda2fusion.model import Project
from easyeda2fusion.utils.io import dump_json


@dataclass
class ValidationReport:
    converted_successfully: bool
    converted_with_warnings: bool
    lossy_conversions: list[str] = field(default_factory=list)
    unresolved_parts: list[str] = field(default_factory=list)
    ambiguous_mappings: list[str] = field(default_factory=list)
    inferred_schematic_items: list[str] = field(default_factory=list)
    manual_review_items: list[str] = field(default_factory=list)
    issues: list["ValidationIssue"] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidationIssue:
    severity: str
    code: str
    message: str
    context: dict[str, Any] = field(default_factory=dict)



def validate_project(
    project: Project,
    match_ctx: MatchContext,
    inference_report: SchematicInferenceReport | None,
) -> ValidationReport:
    lossy_layers = sorted({layer.source_name for layer in project.layers if layer.lossy})
    unresolved = sorted({part.refdes for part in match_ctx.unresolved_parts if str(part.refdes or "").strip()})
    ambiguous = sorted({
        match.refdes
        for match in project.library_matches
        if not match.matched and match.candidates and str(match.refdes or "").strip()
    })

    issues: list[ValidationIssue] = []
    manual_review_issues: list[ValidationIssue] = []

    def add_manual_review(code: str, message: str, context: dict[str, Any] | None = None) -> None:
        manual_review_issues.append(
            ValidationIssue(
                severity="warning",
                code=code,
                message=message,
                context=context or {},
            )
        )

    package_lookup = {package.package_id: package for package in project.packages}
    package_lookup.update({package.name: package for package in project.packages})
    symbol_lookup = {symbol.symbol_id: symbol for symbol in project.symbols}
    device_lookup = {device.device_id: device for device in project.devices}

    component_pin_count: dict[str, int] = {}
    for net in project.nets:
        for node in net.nodes:
            ref = str(node.refdes or "").strip()
            if not ref:
                continue
            component_pin_count[ref] = component_pin_count.get(ref, 0) + 1

    if project.board is None:
        add_manual_review("BOARD_MISSING", "Board missing from conversion output")
    else:
        if not project.board.outline:
            add_manual_review("BOARD_OUTLINE_MISSING", "Board outline missing or unsupported")
        for idx, region in enumerate(project.board.outline, start=1):
            if len(region.points) < 3:
                add_manual_review(
                    "BOARD_OUTLINE_REGION_TOO_SMALL",
                    f"Board outline region {idx} has fewer than 3 points",
                    {"region_index": idx},
                )

    if not project.sheets:
        add_manual_review("SCHEMATIC_SHEETS_MISSING", "No schematic sheets present")

    for component in project.components:
        if not component.device_id:
            add_manual_review(
                "COMPONENT_DEVICE_MAPPING_MISSING",
                f"{component.refdes}: missing device mapping",
                {"refdes": str(component.refdes or "")},
            )
        if not component.package_id:
            add_manual_review(
                "COMPONENT_PACKAGE_MAPPING_MISSING",
                f"{component.refdes}: missing package mapping",
                {"refdes": str(component.refdes or "")},
            )
        if component.device_id and component.device_id.startswith("easyeda_generated:"):
            raw_device = component.device_id.split(":", 1)[1]
            if raw_device not in device_lookup:
                add_manual_review(
                    "GENERATED_DEVICE_MISSING",
                    f"{component.refdes}: generated device {raw_device} missing from project devices",
                    {"refdes": str(component.refdes or ""), "device_id": raw_device},
                )
        if component.package_id and component.package_id not in package_lookup:
            add_manual_review(
                "PACKAGE_NOT_FOUND",
                f"{component.refdes}: package {component.package_id} not found in package set",
                {"refdes": str(component.refdes or ""), "package_id": str(component.package_id or "")},
            )
        if component_pin_count.get(component.refdes, 0) == 0:
            add_manual_review(
                "COMPONENT_ZERO_CONNECTIVITY",
                f"{component.refdes}: zero electrical connections in schematic netlist",
                {"refdes": str(component.refdes or "")},
            )

    for net in project.nets:
        if not net.nodes:
            add_manual_review(
                "NET_EMPTY_CONNECTIVITY",
                f"Net {net.name} has no explicit node connectivity",
                {"net_name": str(net.name or "")},
            )
            continue
        unique_nodes = {(node.refdes, node.pin) for node in net.nodes if node.refdes and node.pin}
        if len(unique_nodes) < 2 and not _is_power_ground_net(net.name):
            add_manual_review(
                "NET_TOO_FEW_PINS",
                f"Net {net.name} has fewer than 2 connected pins",
                {"net_name": str(net.name or ""), "connected_pin_count": len(unique_nodes)},
            )

    for device in project.devices:
        symbol = symbol_lookup.get(device.symbol_id)
        if symbol is None:
            add_manual_review(
                "DEVICE_SYMBOL_MISSING",
                f"Device {device.device_id}: missing symbol {device.symbol_id}",
                {"device_id": str(device.device_id or ""), "symbol_id": str(device.symbol_id or "")},
            )
            continue
        package = package_lookup.get(device.package_id) if device.package_id else None
        symbol_pin_names = {str(pin.pin_name or "").strip() for pin in symbol.pins if str(pin.pin_name or "").strip()}
        symbol_pin_numbers = {str(pin.pin_number or "").strip() for pin in symbol.pins if str(pin.pin_number or "").strip()}
        pad_numbers = {
            str(pad.pad_number).strip()
            for pad in package.pads
        } if package is not None else set()
        for pin_name, pad_name in device.pin_pad_map.items():
            pin_token = str(pin_name or "").strip()
            pad_token = str(pad_name or "").strip()
            if not pin_token or not pad_token:
                add_manual_review(
                    "DEVICE_PIN_PAD_MAPPING_EMPTY",
                    f"Device {device.device_id}: empty pin/pad mapping entry",
                    {"device_id": str(device.device_id or "")},
                )
                continue
            if pin_token not in symbol_pin_names and pin_token not in symbol_pin_numbers:
                add_manual_review(
                    "DEVICE_SYMBOL_PIN_MISSING",
                    f"Device {device.device_id}: pin {pin_token} missing from symbol {symbol.symbol_id}",
                    {
                        "device_id": str(device.device_id or ""),
                        "pin": pin_token,
                        "symbol_id": str(symbol.symbol_id or ""),
                    },
                )
            if package is not None and pad_token not in pad_numbers:
                add_manual_review(
                    "DEVICE_PACKAGE_PAD_MISSING",
                    f"Device {device.device_id}: pad {pad_token} missing from package {package.name}",
                    {
                        "device_id": str(device.device_id or ""),
                        "pad": pad_token,
                        "package_name": str(package.name or ""),
                    },
                )

    for component in project.components:
        if not _is_resistor_array_component(component):
            continue
        package = package_lookup.get(str(component.package_id or ""))
        if package is None:
            add_manual_review(
                "RESISTOR_ARRAY_PACKAGE_MISSING",
                f"{component.refdes}: resistor array package missing",
                {"refdes": str(component.refdes or "")},
            )
            continue
        if len(package.pads) < 4:
            add_manual_review(
                "RESISTOR_ARRAY_TOO_FEW_PADS",
                f"{component.refdes}: resistor array package has too few pads",
                {"refdes": str(component.refdes or ""), "pad_count": len(package.pads)},
            )

    board_nets = _board_net_names(project)
    schematic_nets = {str(net.name or "").strip() for net in project.nets if str(net.name or "").strip()}
    missing_board_nets = sorted(board_nets - schematic_nets)
    for name in missing_board_nets[:200]:
        add_manual_review(
            "BOARD_NET_MISSING_FROM_SCHEMATIC",
            f"Board net {name} not present in schematic nets",
            {"net_name": name},
        )

    inserted_supply = project.metadata.get("supply_symbols_inserted", [])
    supply_mode = str(project.metadata.get("supply_symbols_mode") or "enabled").strip().lower()
    if isinstance(inserted_supply, list):
        inserted_supply_nets = {
            str(item.get("net") or "").strip()
            for item in inserted_supply
            if isinstance(item, dict) and str(item.get("net") or "").strip()
        }
    else:
        inserted_supply_nets = set()
    recognized_power_nets = {
        str(net.name or "").strip()
        for net in project.nets
        if _is_power_ground_net(net.name)
    }
    if supply_mode != "disabled":
        for net_name in sorted(recognized_power_nets):
            if net_name not in inserted_supply_nets:
                add_manual_review(
                    "POWER_NET_SUPPLY_SYMBOL_MISSING",
                    f"Power/GND net {net_name} has no inserted supply symbol",
                    {"net_name": net_name},
                )

    source_instance_total = len(project.components)
    expected_mappable_keys = {
        _component_instance_key(component, ordinal)
        for ordinal, component in enumerate(project.components, start=1)
        if str(component.device_id or "").strip()
    }
    board_instance_keys = _instance_record_keys(project.metadata.get("board_instance_refdes_map"))
    schematic_instance_keys = _instance_record_keys(project.metadata.get("schematic_instance_refdes_map"))
    placed_instance_total = len(board_instance_keys) if board_instance_keys else sum(
        1 for component in project.components if component.device_id
    )
    if board_instance_keys:
        missing_board_keys = sorted(expected_mappable_keys - board_instance_keys)
        unexpected_board_keys = sorted(board_instance_keys - expected_mappable_keys)
        if missing_board_keys or unexpected_board_keys or placed_instance_total != source_instance_total:
            add_manual_review(
                "BOARD_INSTANCE_COUNT_MISMATCH",
                f"Board instance count mismatch: source={source_instance_total} placed={placed_instance_total}",
                {
                    "missing_component_keys": missing_board_keys[:200],
                    "unexpected_component_keys": unexpected_board_keys[:200],
                },
            )
    elif placed_instance_total != source_instance_total:
        add_manual_review(
            "BOARD_INSTANCE_COUNT_MISMATCH",
            f"Board instance count mismatch: source={source_instance_total} placed={placed_instance_total}",
        )
    schematic_emitted_total = len(schematic_instance_keys)
    if schematic_instance_keys:
        missing_schematic_keys = sorted(expected_mappable_keys - schematic_instance_keys)
        unexpected_schematic_keys = sorted(schematic_instance_keys - expected_mappable_keys)
        if missing_schematic_keys or unexpected_schematic_keys:
            add_manual_review(
                "SCHEMATIC_INSTANCE_COUNT_MISMATCH",
                f"Schematic emitted instance count mismatch: expected={len(expected_mappable_keys)} emitted={schematic_emitted_total}",
                {
                    "missing_component_keys": missing_schematic_keys[:200],
                    "unexpected_component_keys": unexpected_schematic_keys[:200],
                },
            )

    duplicate_refdes = sorted(
        refdes
        for refdes, count in _component_refdes_counts(project).items()
        if count > 1
    )
    for refdes in duplicate_refdes:
        add_manual_review(
            "DUPLICATE_COMPONENT_REFDES",
            f"Duplicate component instance refdes detected: {refdes}",
            {"refdes": refdes},
        )

    component_refdes = {str(component.refdes or "").strip() for component in project.components}
    generated_device_ids = {str(component.device_id or "") for component in project.components}
    required_refs_raw = project.metadata.get("required_component_refs")
    if isinstance(required_refs_raw, (list, tuple, set)):
        for ref in required_refs_raw:
            token = str(ref or "").strip()
            if not token:
                continue
            if token in component_refdes:
                continue
            add_manual_review(
                "REQUIRED_COMPONENT_MISSING",
                f"Expected component {token} not present in source component list",
                {"refdes": token},
            )

    required_generated_raw = project.metadata.get("required_generated_devices")
    if isinstance(required_generated_raw, (list, tuple, set)):
        for device_name in required_generated_raw:
            token = str(device_name or "").strip()
            if not token:
                continue
            if any(token in item for item in generated_device_ids):
                continue
            add_manual_review(
                "REQUIRED_GENERATED_DEVICE_MISSING",
                f"Expected generated device {token} not present in mapped component devices",
                {"device_id": token},
            )

    inferred_items: list[str] = []
    if inference_report is not None and inference_report.inferred:
        inferred_items.extend([f"net:{name}" for name in inference_report.inferred_nets])
        inferred_items.extend([f"uncertain_component:{ref}" for ref in inference_report.uncertain_components])
        for item in inference_report.manual_review_items:
            add_manual_review(
                "INFERRED_SCHEMATIC_MANUAL_REVIEW",
                str(item),
                {"source": "inference_report"},
            )

    organization_metrics = (
        project.metadata.get("schematic_organization_metrics", {})
        if isinstance(project.metadata, dict)
        else {}
    )
    if not isinstance(organization_metrics, dict):
        organization_metrics = {}
    overlap_count = int(organization_metrics.get("overlap_count", 0) or 0)
    orphan_label_count = int(organization_metrics.get("orphan_label_count", 0) or 0)
    disconnected_count = int(organization_metrics.get("disconnected_component_count", 0) or 0)
    if overlap_count > 0:
        add_manual_review(
            "SCHEMATIC_ORGANIZATION_OVERLAP",
            f"Schematic organization overlap_count={overlap_count}",
            {"overlap_count": overlap_count},
        )
    if orphan_label_count > 0:
        add_manual_review(
            "SCHEMATIC_ORGANIZATION_ORPHAN_LABELS",
            f"Schematic organization orphan_label_count={orphan_label_count}",
            {"orphan_label_count": orphan_label_count},
        )
    if disconnected_count > 0:
        add_manual_review(
            "SCHEMATIC_ORGANIZATION_DISCONNECTED_COMPONENTS",
            f"Schematic organization disconnected_component_count={disconnected_count}",
            {"disconnected_component_count": disconnected_count},
        )

    draw_metrics = (
        project.metadata.get("schematic_draw_metrics", {})
        if isinstance(project.metadata, dict)
        else {}
    )
    if not isinstance(draw_metrics, dict):
        draw_metrics = {}
    draw_unresolved_anchor_count = int(draw_metrics.get("unresolved_pin_anchor_count", 0) or 0)
    draw_orphan_endpoint_count = int(draw_metrics.get("orphan_wire_endpoint_count", 0) or 0)
    draw_label_only_count = int(draw_metrics.get("label_only_connection_count", 0) or 0)
    draw_disconnected_count = int(draw_metrics.get("disconnected_component_count", 0) or 0)
    if draw_unresolved_anchor_count > 0:
        add_manual_review(
            "SCHEMATIC_DRAW_UNRESOLVED_PIN_ANCHORS",
            f"Schematic draw unresolved_pin_anchor_count={draw_unresolved_anchor_count}",
            {"unresolved_pin_anchor_count": draw_unresolved_anchor_count},
        )
    if draw_orphan_endpoint_count > 0:
        add_manual_review(
            "SCHEMATIC_DRAW_ORPHAN_WIRE_ENDPOINTS",
            f"Schematic draw orphan_wire_endpoint_count={draw_orphan_endpoint_count}",
            {"orphan_wire_endpoint_count": draw_orphan_endpoint_count},
        )
    if draw_label_only_count > 0:
        add_manual_review(
            "SCHEMATIC_DRAW_LABEL_ONLY_CONNECTIONS",
            f"Schematic draw label_only_connection_count={draw_label_only_count}",
            {"label_only_connection_count": draw_label_only_count},
        )
    if draw_disconnected_count > 0:
        add_manual_review(
            "SCHEMATIC_DRAW_DISCONNECTED_COMPONENTS",
            f"Schematic draw disconnected_component_count={draw_disconnected_count}",
            {"disconnected_component_count": draw_disconnected_count},
        )

    net_plan_report = (
        project.metadata.get("schematic_net_attachment_plan", {})
        if isinstance(project.metadata, dict)
        else {}
    )
    if not isinstance(net_plan_report, dict):
        net_plan_report = {}
    label_owner_collision_count = int(net_plan_report.get("label_owner_collision_count", 0) or 0)
    ownerless_label_stub_count = sum(
        1
        for item in net_plan_report.get("pending_label_stubs", [])
        if isinstance(item, dict)
        and str(item.get("path_mode") or "") in {"stub", "stub_fallback"}
        and not str(item.get("owner_refdes") or "").strip()
    )
    if ownerless_label_stub_count > 0:
        add_manual_review(
            "SCHEMATIC_LABEL_OWNER_MISSING",
            f"Schematic label ownership unresolved for {ownerless_label_stub_count} pending stub label(s)",
            {"ownerless_label_stub_count": ownerless_label_stub_count},
        )

    for layer_name in lossy_layers:
        issues.append(
            ValidationIssue(
                severity="warning",
                code="LOSSY_LAYER_MAPPING",
                message=f"Lossy layer mapping: {layer_name}",
                context={"source_layer": layer_name},
            )
        )
    for refdes in unresolved:
        issues.append(
            ValidationIssue(
                severity="warning",
                code="UNRESOLVED_PART",
                message=f"{refdes}: unresolved part mapping",
                context={"refdes": refdes},
            )
        )
    for refdes in ambiguous:
        issues.append(
            ValidationIssue(
                severity="warning",
                code="AMBIGUOUS_MAPPING",
                message=f"{refdes}: ambiguous library mapping",
                context={"refdes": refdes},
            )
        )
    for item in inferred_items:
        issue_code = "INFERRED_SCHEMATIC_ITEM"
        issues.append(
            ValidationIssue(
                severity="info",
                code=issue_code,
                message=item,
                context={"item": item},
            )
        )
    issues.extend(manual_review_issues)
    issues = _dedupe_validation_issues(issues)
    manual_review_items = sorted({issue.message for issue in manual_review_issues})

    has_warnings = bool(lossy_layers or unresolved or ambiguous or manual_review_items)

    report = ValidationReport(
        converted_successfully=True,
        converted_with_warnings=has_warnings,
        lossy_conversions=lossy_layers,
        unresolved_parts=unresolved,
        ambiguous_mappings=ambiguous,
        inferred_schematic_items=inferred_items,
        manual_review_items=manual_review_items,
        issues=issues,
        metrics={
            "component_count": len(project.components),
            "net_count": len(project.nets),
            "sheet_count": len(project.sheets),
            "device_count": len(project.devices),
            "package_count": len(project.packages),
            "layer_count": len(project.layers),
            "board_track_count": len(project.board.tracks) if project.board else 0,
            "board_via_count": len(project.board.vias) if project.board else 0,
            "board_pad_count": len(project.board.pads) if project.board else 0,
            "source_component_instance_count": source_instance_total,
            "placed_component_instance_count": placed_instance_total,
            "board_emitted_component_instance_count": placed_instance_total,
            "schematic_emitted_component_instance_count": schematic_emitted_total,
            "schematic_expected_mappable_component_instance_count": len(expected_mappable_keys),
            "recognized_power_ground_nets": len(recognized_power_nets),
            "inserted_supply_symbols": len(inserted_supply_nets),
            "schematic_organization_block_count": int(organization_metrics.get("block_count", 0) or 0),
            "schematic_organization_repeated_channel_groups": int(organization_metrics.get("repeated_channel_groups", 0) or 0),
            "schematic_organization_crossing_risk_score": int(organization_metrics.get("crossing_risk_score", 0) or 0),
            "schematic_organization_overlap_count": overlap_count,
            "schematic_organization_orphan_label_count": orphan_label_count,
            "schematic_organization_disconnected_component_count": disconnected_count,
            "schematic_organization_power_net_count": len(organization_metrics.get("recognized_power_nets", [])),
            "schematic_organization_ground_net_count": len(organization_metrics.get("recognized_ground_nets", [])),
            "schematic_draw_symbol_count": int(draw_metrics.get("symbol_count", 0) or 0),
            "schematic_draw_connected_pin_count": int(draw_metrics.get("connected_pin_count", 0) or 0),
            "schematic_draw_wire_segment_count": int(draw_metrics.get("wire_segment_count", 0) or 0),
            "schematic_draw_junction_count": int(draw_metrics.get("junction_count", 0) or 0),
            "schematic_draw_orphan_wire_endpoint_count": draw_orphan_endpoint_count,
            "schematic_draw_unresolved_pin_anchor_count": draw_unresolved_anchor_count,
            "schematic_draw_label_only_connection_count": draw_label_only_count,
            "schematic_draw_disconnected_component_count": draw_disconnected_count,
            "schematic_label_owner_collision_count": label_owner_collision_count,
            "schematic_ownerless_label_stub_count": ownerless_label_stub_count,
            "validation_issue_count": len(issues),
            "manual_review_issue_count": len(manual_review_items),
        },
    )
    return report


def _is_power_ground_net(name: str) -> bool:
    token = "".join(ch for ch in str(name or "").upper() if ch.isalnum())
    if not token:
        return False
    if token in {
        "GND",
        "AGND",
        "DGND",
        "PGND",
        "EARTH",
        "CHASSIS",
        "3V3",
        "33V",
        "5V",
        "5V0",
        "12V",
        "VCC",
        "VDD",
        "VSS",
        "VBAT",
        "VIN",
        "AVDD",
        "DVDD",
    }:
        return True
    return token.startswith("VCC") or token.startswith("VDD")


def _is_resistor_array_component(component) -> bool:
    ref = str(component.refdes or "").upper()
    source = str(component.source_name or "").upper()
    attrs = component.attributes if isinstance(component.attributes, dict) else {}
    blob = " ".join(
        str(item or "").upper()
        for item in (
            source,
            attrs.get("Name"),
            attrs.get("Device"),
            attrs.get("Footprint"),
            attrs.get("Package"),
            attrs.get("component_class"),
        )
    )
    return (
        "RES-ARRAY" in blob
        or "RESISTORARRAY" in blob
        or "RESISTOR NETWORK" in blob
        or ref.startswith(("RN", "RA"))
    )


def _board_net_names(project: Project) -> set[str]:
    if project.board is None:
        return set()
    names = {
        str(track.net or "").strip()
        for track in project.board.tracks
        if str(track.net or "").strip()
    }
    names.update(
        str(via.net or "").strip()
        for via in project.board.vias
        if str(via.net or "").strip()
    )
    names.update(
        str(region.net or "").strip()
        for region in project.board.regions
        if str(region.net or "").strip()
    )
    return names


def _component_refdes_counts(project: Project) -> dict[str, int]:
    counts: dict[str, int] = {}
    for component in project.components:
        ref = str(component.refdes or "").strip()
        if not ref:
            continue
        counts[ref] = counts.get(ref, 0) + 1
    return counts


def _component_instance_key(component: Any, ordinal: int) -> str:
    return component_instance_key(component, ordinal)


def _instance_record_keys(raw_value: Any) -> set[str]:
    if not isinstance(raw_value, list):
        return set()
    keys: set[str] = set()
    for item in raw_value:
        if not isinstance(item, dict):
            continue
        key = str(item.get("source_component_key") or "").strip()
        if key:
            keys.add(key)
    return keys


def _dedupe_validation_issues(issues: list[ValidationIssue]) -> list[ValidationIssue]:
    seen: set[tuple[str, str, str, str]] = set()
    out: list[ValidationIssue] = []
    for issue in issues:
        signature = (
            str(issue.severity or ""),
            str(issue.code or ""),
            str(issue.message or ""),
            json.dumps(issue.context, sort_keys=True, ensure_ascii=True),
        )
        if signature in seen:
            continue
        seen.add(signature)
        out.append(issue)
    return out


def write_validation_report(report: ValidationReport, out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "validation_report.json"
    text_path = out_dir / "validation_report.txt"

    dump_json(json_path, asdict(report))

    lines = [
        "Validation Report",
        "",
        f"converted_successfully: {report.converted_successfully}",
        f"converted_with_warnings: {report.converted_with_warnings}",
        f"lossy_conversions: {len(report.lossy_conversions)}",
        f"unresolved_parts: {len(report.unresolved_parts)}",
        f"ambiguous_mappings: {len(report.ambiguous_mappings)}",
        f"inferred_schematic_items: {len(report.inferred_schematic_items)}",
        f"manual_review_items: {len(report.manual_review_items)}",
        f"issues: {len(report.issues)}",
        "",
        "Metrics:",
    ]
    for key, value in sorted(report.metrics.items()):
        lines.append(f"- {key}: {value}")

    if report.issues:
        lines.append("")
        lines.append("Issues:")
        for issue in report.issues:
            lines.append(f"- [{issue.severity}] {issue.code}: {issue.message}")

    if report.manual_review_items:
        lines.append("")
        lines.append("Manual Review Items:")
        for item in report.manual_review_items:
            lines.append(f"- {item}")

    text_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"json": json_path, "text": text_path}
