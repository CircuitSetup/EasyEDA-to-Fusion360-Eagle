from __future__ import annotations

import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from easyeda2fusion.model import (
    ParsedDocument,
    ParsedSource,
    Severity,
    SourceFormat,
    project_event,
)
from easyeda2fusion.parsers.base import EasyEDAParser
from easyeda2fusion.utils.io import load_json

_NUM_RE = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")
_LEGACY_BOARD_DOC_TYPES = {"3", "pcb", "board", "layout"}
_LEGACY_SCHEMATIC_DOC_TYPES = {"1", "2", "schematic", "sch"}


class EasyEDAStdParser(EasyEDAParser):
    source_format = SourceFormat.EASYEDA_STD

    def can_parse(self, payload: dict[str, Any]) -> bool:
        fmt = str(payload.get("format", "")).lower()
        editor = str(payload.get("editor", "")).lower()
        version = str(payload.get("editorVersion", "")).lower()
        if "pro" in fmt or "pro" in version:
            return False
        if fmt in {"easyeda_std", "easyeda_lite", "easyeda standard", "std"}:
            return True
        if editor in {"easyeda", "easyeda-lite", "easyeda standard"}:
            return True
        return "head" in payload and "shape" in payload

    def parse_files(self, paths: list[Path]) -> ParsedSource:
        documents: list[ParsedDocument] = []
        layers: list[dict[str, Any]] = []
        rules: list[dict[str, Any]] = []
        metadata: dict[str, Any] = {}
        events = []

        for path in paths:
            payload = load_json(path)
            if not isinstance(payload, dict):
                events.append(
                    project_event(
                        Severity.ERROR,
                        "STD_NON_OBJECT_PAYLOAD",
                        f"Expected JSON object in {path.name}",
                        {"path": str(path)},
                    )
                )
                continue

            if not self.can_parse(payload):
                events.append(
                    project_event(
                        Severity.WARNING,
                        "STD_FORMAT_UNCERTAIN",
                        f"{path.name} did not cleanly match Standard/Lite signature",
                        {"path": str(path)},
                    )
                )

            merged_meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
            metadata.update(merged_meta)
            self._collect_legacy_coordinate_metadata(payload, metadata)

            if "layers" in payload and isinstance(payload["layers"], list):
                layers.extend(item for item in payload["layers"] if isinstance(item, dict))

            if "rules" in payload and isinstance(payload["rules"], list):
                rules.extend(item for item in payload["rules"] if isinstance(item, dict))

            self._collect_docs_from_payload(payload, path.name, documents)

        if "coordinate_scale_to_mm" not in metadata and "unit" not in metadata:
            events.append(
                project_event(
                    Severity.WARNING,
                    "STD_SCALE_ASSUMED",
                    "No explicit coordinate scale found; defaulting to 10-mil Standard scale",
                )
            )

        if not documents:
            events.append(
                project_event(
                    Severity.ERROR,
                    "STD_NO_DOCUMENTS",
                    "No schematic or board documents were found in Standard/Lite input",
                )
            )

        return ParsedSource(
            source_format=self.source_format,
            input_files=[str(path) for path in paths],
            documents=documents,
            layers=layers,
            rules=rules,
            metadata=metadata,
            events=events,
        )

    def _collect_docs_from_payload(
        self,
        payload: dict[str, Any],
        default_name: str,
        sink: list[ParsedDocument],
    ) -> None:
        schematics = payload.get("schematics")
        if isinstance(schematics, list):
            for idx, sch in enumerate(schematics):
                if not isinstance(sch, dict):
                    continue
                sink.append(
                    ParsedDocument(
                        doc_type="schematic",
                        name=str(sch.get("name", f"sheet_{idx + 1}")),
                        raw_objects=self._object_list_from_doc(sch),
                        metadata={k: v for k, v in sch.items() if k not in {"objects", "shape"}},
                    )
                )

        if isinstance(payload.get("schematic"), dict):
            sch_doc = payload["schematic"]
            sink.append(
                ParsedDocument(
                    doc_type="schematic",
                    name=str(sch_doc.get("name", f"{default_name}_schematic")),
                    raw_objects=self._object_list_from_doc(sch_doc),
                    metadata={k: v for k, v in sch_doc.items() if k not in {"objects", "shape"}},
                )
            )

        board_doc = payload.get("board") if isinstance(payload.get("board"), dict) else payload.get("pcb")
        if isinstance(board_doc, dict):
            sink.append(
                ParsedDocument(
                    doc_type="board",
                    name=str(board_doc.get("name", f"{default_name}_board")),
                    raw_objects=self._object_list_from_doc(board_doc),
                    metadata={k: v for k, v in board_doc.items() if k not in {"objects", "shape"}},
                )
            )

        if "shape" in payload and isinstance(payload["shape"], list):
            raw_objects = self._decode_shape_list(payload["shape"])
            doc_type = self._guess_legacy_doc_type(payload, raw_objects)
            sink.append(
                ParsedDocument(
                    doc_type=doc_type,
                    name=str(payload.get("name", default_name)),
                    raw_objects=raw_objects,
                    metadata={k: v for k, v in payload.items() if k not in {"shape", "objects"}},
                )
            )

    def _object_list_from_doc(self, doc: dict[str, Any]) -> list[dict[str, Any]]:
        if isinstance(doc.get("objects"), list):
            return [item for item in doc["objects"] if isinstance(item, dict)]
        if isinstance(doc.get("shape"), list):
            return self._decode_shape_list(doc["shape"])
        return []

    def _decode_shape_list(self, raw_items: list[Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        used_refdes: set[str] = set()
        for item in raw_items:
            if isinstance(item, dict):
                out.append(item)
                continue
            if isinstance(item, str):
                out.extend(self._decode_legacy_shape_record(item, used_refdes))
        return out

    def _decode_legacy_shape_record(
        self,
        record: str,
        used_refdes: set[str],
    ) -> list[dict[str, Any]]:
        raw = str(record or "").strip()
        if not raw:
            return []
        if raw.startswith("LIB~"):
            return self._decode_legacy_lib_record(raw, used_refdes)

        parts = raw.split("~")
        kind = str(parts[0]).strip().upper()
        if not kind:
            return []

        if kind == "TRACK":
            return self._decode_track_tokens(parts)
        if kind == "VIA":
            via = self._decode_via_tokens(parts)
            return [via] if via is not None else []
        if kind == "PAD":
            pad = self._decode_pad_tokens(parts)
            return [pad] if pad is not None else []
        if kind == "HOLE":
            hole = self._decode_hole_tokens(parts)
            return [hole] if hole is not None else []
        if kind in {"COPPERAREA", "SOLIDREGION"}:
            region = self._decode_region_tokens(parts)
            return [region] if region is not None else []
        if kind == "RECT":
            rect_region = self._decode_rect_tokens(parts)
            return [rect_region] if rect_region is not None else []
        if kind == "TEXT":
            text = self._decode_text_tokens(parts)
            return [text] if text is not None else []
        if kind == "SVGNODE":
            mechanical = self._decode_svgnode_tokens(parts)
            return [mechanical] if mechanical is not None else []

        return [{"type": "legacy_raw", "legacy_kind": kind.lower(), "raw": raw}]

    def _decode_track_tokens(self, parts: list[str]) -> list[dict[str, Any]]:
        if len(parts) < 5:
            return []
        width = _safe_float(parts[1], 0.2)
        layer = str(parts[2]).strip() or "1"
        net = _clean_optional(parts[3])
        point_pairs = _parse_coordinate_pairs(parts[4])
        if len(point_pairs) < 2:
            return []

        gge_id = _token(parts, 5)
        out: list[dict[str, Any]] = []
        for idx in range(len(point_pairs) - 1):
            (x1, y1) = point_pairs[idx]
            (x2, y2) = point_pairs[idx + 1]
            out.append(
                {
                    "type": "track",
                    "x1": x1,
                    "y1": y1,
                    "x2": x2,
                    "y2": y2,
                    "width": width,
                    "layer": layer,
                    "net": net,
                    "id": gge_id,
                    "segment_index": idx,
                }
            )
        return out

    def _decode_via_tokens(self, parts: list[str]) -> dict[str, Any] | None:
        # Common legacy shape: VIA~x~y~diameter~net~drill~id~locked
        if len(parts) < 4:
            return None
        x = _safe_float(parts[1], 0.0)
        y = _safe_float(parts[2], 0.0)
        diameter = _safe_float(parts[3], 0.6)
        net = _clean_optional(_token(parts, 4))
        drill_raw = _safe_float(_token(parts, 5), 0.0)
        if drill_raw <= 0.0:
            drill = max(0.2, diameter * 0.5)
        else:
            drill = drill_raw
            if drill_raw <= (diameter * 0.35) and (drill_raw * 2.0) <= (diameter * 1.1):
                drill = drill_raw * 2.0
        return {
            "type": "via",
            "x": x,
            "y": y,
            "diameter": diameter,
            "drill": drill,
            "net": net,
            "id": _token(parts, 6),
        }

    def _decode_pad_tokens(self, parts: list[str]) -> dict[str, Any] | None:
        if len(parts) < 10:
            return None
        rotation_raw = _token(parts, 11)
        rotation = _safe_float(rotation_raw, 0.0) if rotation_raw else 0.0
        width = max(_safe_float(parts[4], 1.0), 0.01)
        height = max(_safe_float(parts[5], 1.0), 0.01)
        drill = _legacy_pad_drill(parts, width=width, height=height)
        pad = {
            "type": "pad",
            "shape": str(parts[1]).strip().lower() or "rect",
            "x": _safe_float(parts[2], 0.0),
            "y": _safe_float(parts[3], 0.0),
            "width": width,
            "height": height,
            "layer": str(parts[6]).strip() or "1",
            "net": _clean_optional(parts[7]),
            "number": str(parts[8]).strip(),
            "rotation": rotation,
            "id": _token(parts, 12),
            "plated": str(_token(parts, 15)).strip().upper() == "Y",
        }
        pad["drill"] = drill if drill > 0.0 else None
        return pad

    def _decode_hole_tokens(self, parts: list[str]) -> dict[str, Any] | None:
        if len(parts) < 4:
            return None
        return {
            "type": "hole",
            "x": _safe_float(parts[1], 0.0),
            "y": _safe_float(parts[2], 0.0),
            "drill": max(_safe_float(parts[3], 0.0), 0.0),
            "id": _token(parts, 4),
            "plated": False,
        }

    def _decode_region_tokens(self, parts: list[str]) -> dict[str, Any] | None:
        if len(parts) < 5:
            return None
        layer = str(parts[2]).strip() or "1"
        net = _clean_optional(parts[3])
        point_sets: list[list[tuple[float, float]]] = []
        for idx in (10, 11, 4):
            point_sets.extend(_parse_region_point_sets(_token(parts, idx)))
        selected = _select_primary_region_points(point_sets)
        points = [[x, y] for (x, y) in selected]
        if len(points) < 3:
            return None
        return {
            "type": "region",
            "layer": layer,
            "net": net,
            "points": points,
            "id": _token(parts, 7),
        }

    def _decode_rect_tokens(self, parts: list[str]) -> dict[str, Any] | None:
        if len(parts) < 6:
            return None
        x = _safe_float(parts[1], 0.0)
        y = _safe_float(parts[2], 0.0)
        w = _safe_float(parts[3], 0.0)
        h = _safe_float(parts[4], 0.0)
        points = [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]
        return {
            "type": "region",
            "layer": str(parts[5]).strip() or "1",
            "points": points,
            "id": _token(parts, 6),
        }

    def _decode_text_tokens(self, parts: list[str]) -> dict[str, Any] | None:
        if len(parts) < 11:
            return None
        size_from_token9 = _safe_float(_token(parts, 9), 0.0)
        size = size_from_token9 if size_from_token9 > 0.0 else _safe_float(_token(parts, 4), 1.2)
        item = {
            "type": "text",
            "kind": _token(parts, 1),
            "x": _safe_float(parts[2], 0.0),
            "y": _safe_float(parts[3], 0.0),
            "size": max(size, 0.1),
            "rotation": _safe_float(parts[5], 0.0),
            "mirrored": str(_token(parts, 6)).strip() == "1",
            "layer": str(parts[7]).strip() or "3",
            "text": _token(parts, 10),
            "id": _token(parts, 13),
        }
        return item

    def _decode_svgnode_tokens(self, parts: list[str]) -> dict[str, Any] | None:
        raw_payload = _token(parts, 1)
        if not raw_payload:
            return None
        layer_match = re.search(r'"layerid"\s*:\s*"([^"]+)"', raw_payload)
        node_match = re.search(r'"nodeName"\s*:\s*"([^"]+)"', raw_payload)
        gid_match = re.search(r'"gId"\s*:\s*"([^"]+)"', raw_payload)
        return {
            "type": "mechanical",
            "layer": layer_match.group(1) if layer_match else "3",
            "kind": node_match.group(1) if node_match else "svgnode",
            "id": gid_match.group(1) if gid_match else "",
            "raw": raw_payload,
        }

    def _decode_legacy_lib_record(self, record: str, used_refdes: set[str]) -> list[dict[str, Any]]:
        fragments = [frag for frag in record.split("#@$") if frag]
        if not fragments:
            return []

        header = fragments[0].split("~")
        if len(header) < 4:
            return [{"type": "legacy_raw", "legacy_kind": "lib", "raw": record}]

        component_x = _safe_float(_token(header, 1), 0.0)
        component_y = _safe_float(_token(header, 2), 0.0)
        component_rotation = _safe_float(_token(header, 4), 0.0)
        component_id = _token(header, 6)
        package_uuid = _token(header, 8)
        attributes = _parse_backtick_attributes(_token(header, 3))

        package_name = _first_non_empty(
            _attr_lookup(attributes, "package", "Package", "Footprint", "3DModel"),
            package_uuid,
        )
        source_name = _first_non_empty(
            _attr_lookup(attributes, "spiceSymbolName", "name", "Name"),
            package_name,
            component_id,
        )
        value = _first_non_empty(
            _attr_lookup(attributes, "value", "Value"),
            _attr_lookup(attributes, "Manufacturer Part", "BOM_Manufacturer Part"),
        )
        mpn = _first_non_empty(
            _attr_lookup(attributes, "Manufacturer Part", "BOM_Manufacturer Part"),
            _attr_lookup(attributes, "Supplier Part"),
        )
        manufacturer = _attr_lookup(attributes, "Manufacturer")

        text_refdes_candidates: list[str] = []
        pad_records_abs: list[dict[str, Any]] = []
        pad_net_nodes: dict[str, list[str]] = defaultdict(list)
        package_outline: list[dict[str, Any]] = []

        for child in fragments[1:]:
            child_parts = child.split("~")
            kind = str(_token(child_parts, 0)).strip().upper()
            if kind == "TEXT":
                text_kind = str(_token(child_parts, 1)).strip().upper()
                text_value = str(_token(child_parts, 10)).strip()
                if text_kind == "P" and _looks_like_refdes(text_value):
                    text_refdes_candidates.append(text_value)
                if text_kind in {"P", "N"}:
                    # P/N records represent logical NAME/VALUE fields in legacy
                    # footprint payloads. Emitting them as fixed silk text causes
                    # duplicate, non-movable designators/values in Fusion/EAGLE.
                    continue
                text_item = self._decode_text_tokens(child_parts)
                if text_item is not None:
                    outline_text = _text_to_local_outline(
                        text_item=text_item,
                        origin_x=component_x,
                        origin_y=component_y,
                        rotation_deg=component_rotation,
                    )
                    if outline_text is not None:
                        package_outline.append(outline_text)
                continue

            if kind == "PAD":
                pad_abs = self._decode_pad_tokens(child_parts)
                if pad_abs is None:
                    continue
                pad_records_abs.append(pad_abs)
                net_name = str(pad_abs.get("net") or "").strip()
                pin_number = str(pad_abs.get("number") or "").strip()
                if net_name and pin_number:
                    pad_net_nodes[net_name].append(pin_number)
                continue

            if kind == "TRACK":
                outline_track = _track_to_local_outline(
                    child_parts=child_parts,
                    origin_x=component_x,
                    origin_y=component_y,
                    rotation_deg=component_rotation,
                )
                if outline_track is not None:
                    package_outline.append(outline_track)
                continue

            if kind == "HOLE":
                outline_hole = _hole_to_local_outline(
                    child_parts=child_parts,
                    origin_x=component_x,
                    origin_y=component_y,
                    rotation_deg=component_rotation,
                )
                if outline_hole is not None:
                    package_outline.append(outline_hole)
                continue

        refdes = _first_non_empty(
            _attr_lookup(attributes, "Designator", "pre", "Ref", "Reference"),
            *text_refdes_candidates,
        )
        if not _looks_like_refdes(refdes):
            inferred = _infer_refdes_from_pad_nets(pad_net_nodes.keys())
            refdes = inferred if _looks_like_refdes(inferred) else ""
        if not _looks_like_refdes(refdes):
            fallback_prefix = _first_non_empty(_attr_lookup(attributes, "spicePre"), "U")
            fallback_prefix = re.sub(r"[^A-Za-z]+", "", str(fallback_prefix)) or "U"
            suffix = re.sub(r"[^A-Za-z0-9]+", "", component_id or package_uuid or "AUTO")[-6:] or "AUTO"
            refdes = f"{fallback_prefix}{suffix}"
        refdes = _unique_refdes(refdes, used_refdes)
        used_refdes.add(refdes)

        for pad_abs in pad_records_abs:
            # Preserve source instance linkage so downstream normalization can
            # score package-frame variants against this exact component instead
            # of the full-board pad cloud.
            pad_abs["component_refdes"] = refdes
            pad_abs["source_instance_id"] = component_id

        component_obj: dict[str, Any] = {
            "type": "component",
            "id": component_id,
            "source_instance_id": component_id,
            "refdes": refdes,
            "x": component_x,
            "y": component_y,
            "rotation": component_rotation,
            "side": "top",
            "source_name": source_name,
            "part_name": source_name,
            "value": value,
            "manufacturer": manufacturer,
            "mpn": mpn,
            "attributes": attributes,
        }
        if package_name:
            component_obj["package"] = package_name
            component_obj["package_id"] = package_name
            component_obj["attributes"]["package_name"] = package_name
        if package_uuid:
            component_obj["attributes"]["package_uuid"] = package_uuid

        objects: list[dict[str, Any]] = [component_obj]
        objects.extend(pad_records_abs)

        for net_name, pin_numbers in sorted(pad_net_nodes.items()):
            unique_pins = sorted({pin for pin in pin_numbers if pin})
            if not unique_pins:
                continue
            objects.append(
                {
                    "type": "net",
                    "name": net_name,
                    "nodes": [{"refdes": refdes, "pin": pin} for pin in unique_pins],
                }
            )

        if package_name and pad_records_abs:
            package_pads = [
                _pad_to_package_local(
                    pad=pad,
                    origin_x=component_x,
                    origin_y=component_y,
                    rotation_deg=component_rotation,
                )
                for pad in pad_records_abs
            ]
            objects.append(
                {
                    "type": "package",
                    "id": package_name,
                    "name": package_name,
                    "pads": package_pads,
                    "outline": package_outline,
                }
            )

        return objects

    def _collect_legacy_coordinate_metadata(self, payload: dict[str, Any], metadata: dict[str, Any]) -> None:
        shape = payload.get("shape")
        if not isinstance(shape, list):
            return
        if not any(isinstance(item, str) for item in shape):
            return

        head = payload.get("head")
        if not isinstance(head, dict):
            return
        doc_type = str(payload.get("docType") or head.get("docType") or "").strip().lower()
        if doc_type not in _LEGACY_BOARD_DOC_TYPES:
            return

        x_raw = _safe_float(head.get("x"), 0.0)
        y_raw = _safe_float(head.get("y"), 0.0)
        if abs(x_raw) < 1e-9 and abs(y_raw) < 1e-9:
            return

        metadata.setdefault("origin_raw", {"x": x_raw, "y": y_raw})
        metadata.setdefault("y_axis_inverted", True)
        metadata.setdefault("legacy_shape_string_mode", True)

    def _guess_legacy_doc_type(
        self,
        payload: dict[str, Any],
        raw_objects: list[dict[str, Any]],
    ) -> str:
        head = payload.get("head") if isinstance(payload.get("head"), dict) else {}
        declared = str(payload.get("docType") or head.get("docType") or "").strip().lower()
        if declared in _LEGACY_BOARD_DOC_TYPES:
            return "board"
        if declared in _LEGACY_SCHEMATIC_DOC_TYPES:
            return "schematic"

        object_types = {str(obj.get("type", "")).strip().lower() for obj in raw_objects if isinstance(obj, dict)}
        board_markers = {"pad", "via", "hole", "region", "outline"}
        if object_types.intersection(board_markers):
            return "board"
        return "schematic"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _token(parts: list[str], index: int) -> str:
    if index < 0 or index >= len(parts):
        return ""
    return str(parts[index])


def _clean_optional(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _parse_coordinate_pairs(payload: str) -> list[tuple[float, float]]:
    numbers = [float(token) for token in _NUM_RE.findall(str(payload or ""))]
    points: list[tuple[float, float]] = []
    for idx in range(0, len(numbers) - 1, 2):
        points.append((numbers[idx], numbers[idx + 1]))
    return points


def _parse_region_point_sets(payload: str) -> list[list[tuple[float, float]]]:
    raw = str(payload or "").strip()
    if not raw:
        return []

    candidates = [match.group(1) for match in re.finditer(r'"(M[^"]+)"', raw, flags=re.IGNORECASE)]
    if not candidates:
        candidates = [raw]

    out: list[list[tuple[float, float]]] = []
    for candidate in candidates:
        contours = [match.group(0) for match in re.finditer(r"M[^M]*(?:Z|$)", candidate, flags=re.IGNORECASE)]
        if not contours:
            contours = [candidate]
        for contour in contours:
            points = _normalize_polygon_points(_parse_coordinate_pairs(contour))
            if len(points) >= 3:
                out.append(points)
    return out


def _normalize_polygon_points(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not points:
        return []
    normalized: list[tuple[float, float]] = [points[0]]
    for x, y in points[1:]:
        px, py = normalized[-1]
        if abs(x - px) < 1e-9 and abs(y - py) < 1e-9:
            continue
        normalized.append((x, y))
    if len(normalized) > 1:
        fx, fy = normalized[0]
        lx, ly = normalized[-1]
        if abs(fx - lx) < 1e-9 and abs(fy - ly) < 1e-9:
            normalized.pop()
    return normalized


def _polygon_abs_area(points: list[tuple[float, float]]) -> float:
    if len(points) < 3:
        return 0.0
    acc = 0.0
    for idx, (x1, y1) in enumerate(points):
        x2, y2 = points[(idx + 1) % len(points)]
        acc += (x1 * y2) - (x2 * y1)
    return abs(acc) * 0.5


def _select_primary_region_points(point_sets: list[list[tuple[float, float]]]) -> list[tuple[float, float]]:
    if not point_sets:
        return []
    scored = [(pts, _polygon_abs_area(pts)) for pts in point_sets]
    best_area = max(area for _, area in scored)
    near_max = [pts for pts, area in scored if area >= (best_area * 0.98)]
    return max(
        near_max,
        key=lambda pts: (len(pts), _polygon_abs_area(pts)),
    )


def _parse_backtick_attributes(blob: str) -> dict[str, str]:
    tokens = str(blob or "").split("`")
    out: dict[str, str] = {}
    for idx in range(0, len(tokens) - 1, 2):
        key = str(tokens[idx]).strip()
        if not key:
            continue
        out[key] = str(tokens[idx + 1]).strip()
    return out


def _attr_lookup(attrs: dict[str, str], *keys: str) -> str:
    if not attrs:
        return ""
    if not keys:
        return ""
    lower_map = {str(k).strip().lower(): v for k, v in attrs.items()}
    for key in keys:
        token = str(key or "").strip()
        if not token:
            continue
        if token in attrs and str(attrs[token]).strip():
            return str(attrs[token]).strip()
        lowered = token.lower()
        if lowered in lower_map and str(lower_map[lowered]).strip():
            return str(lower_map[lowered]).strip()
    return ""


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _looks_like_refdes(value: Any) -> bool:
    token = str(value or "").strip()
    if not token:
        return False
    if any(ch in token for ch in (" ", "/", "\\")):
        return False
    if len(token) > 80:
        return False
    if not token[0].isalpha():
        return False
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_\-]*", token))


def _infer_refdes_from_pad_nets(net_names: Any) -> str:
    prefix_candidates: list[str] = []
    for raw_name in net_names:
        name = str(raw_name or "").strip()
        if not name:
            continue
        match = re.match(r"^([A-Za-z]+\d+)[_\-].*", name)
        if match:
            prefix_candidates.append(match.group(1))
    unique = sorted(set(prefix_candidates))
    if len(unique) == 1:
        return unique[0]
    return ""


def _unique_refdes(base_refdes: str, used_refdes: set[str]) -> str:
    base = str(base_refdes or "").strip() or "U_AUTO"
    candidate = base
    suffix = 2
    while candidate in used_refdes:
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


def _legacy_pad_drill(parts: list[str], width: float, height: float) -> float:
    primary = _safe_float(_token(parts, 9), 0.0)
    alternate = _safe_float(_token(parts, 13), 0.0)

    # Legacy string PAD records sometimes carry plated-hole diameter in token 13.
    # Prefer that value when it is physically plausible relative to copper size.
    if alternate > 0.0:
        max_dim = max(float(width), float(height))
        plausible_alt = alternate <= (max_dim * 1.25)
        if primary <= 0.0 and plausible_alt:
            return alternate
        if plausible_alt and alternate >= (primary * 1.4):
            return alternate
    if primary <= 0.0:
        return 0.0

    max_dim = max(float(width), float(height))
    doubled = primary * 2.0
    if doubled <= (max_dim * 1.25) and primary <= (max_dim * 0.4):
        # When token 13 is missing, token 9 in legacy PAD records is often a
        # radius-like scalar. Convert to diameter if it remains physically
        # plausible relative to pad copper size.
        return doubled
    return primary


def _track_to_local_outline(
    child_parts: list[str],
    origin_x: float,
    origin_y: float,
    rotation_deg: float,
) -> dict[str, Any] | None:
    if len(child_parts) < 5:
        return None
    points_abs = _parse_coordinate_pairs(_token(child_parts, 4))
    if len(points_abs) < 2:
        return None
    points_local = _localize_points(
        points_abs,
        origin_x=origin_x,
        origin_y=origin_y,
        rotation_deg=rotation_deg,
    )
    if len(points_local) < 2:
        return None
    return {
        "kind": "wire_path",
        "layer": str(_token(child_parts, 2)).strip() or "3",
        "width_local": max(_safe_float(_token(child_parts, 1), 0.2), 0.01),
        "points": [{"x_local": px, "y_local": py} for (px, py) in points_local],
    }


def _text_to_local_outline(
    text_item: dict[str, Any],
    origin_x: float,
    origin_y: float,
    rotation_deg: float,
) -> dict[str, Any] | None:
    text = str(text_item.get("text") or "").strip()
    if not text:
        return None
    x = _safe_float(text_item.get("x"), 0.0)
    y = _safe_float(text_item.get("y"), 0.0)
    lx, ly = _localize_point(
        x=x,
        y=y,
        origin_x=origin_x,
        origin_y=origin_y,
        rotation_deg=rotation_deg,
    )
    local_rotation = (_safe_float(text_item.get("rotation"), 0.0) - float(rotation_deg or 0.0)) % 360.0
    size_mm = text_item.get("size_mm")
    size_local = max(_safe_float(text_item.get("size"), 0.6), 0.2)
    item: dict[str, Any] = {
        "kind": "text",
        "layer": str(text_item.get("layer") or "3"),
        "text": text,
        "x_local": lx,
        "y_local": ly,
        "size_local": size_local,
        "rotation_deg": local_rotation,
    }
    if size_mm is not None:
        item["size_mm"] = max(_safe_float(size_mm, size_local), 0.2)
    return item


def _hole_to_local_outline(
    child_parts: list[str],
    origin_x: float,
    origin_y: float,
    rotation_deg: float,
) -> dict[str, Any] | None:
    if len(child_parts) < 4:
        return None
    x = _safe_float(_token(child_parts, 1), 0.0)
    y = _safe_float(_token(child_parts, 2), 0.0)
    drill = max(_safe_float(_token(child_parts, 3), 0.0), 0.0)
    if drill <= 0.0:
        return None
    lx, ly = _localize_point(
        x=x,
        y=y,
        origin_x=origin_x,
        origin_y=origin_y,
        rotation_deg=rotation_deg,
    )
    return {
        "kind": "hole",
        "x_local": lx,
        "y_local": ly,
        "drill_local": drill,
    }


def _localize_points(
    points_abs: list[tuple[float, float]],
    origin_x: float,
    origin_y: float,
    rotation_deg: float,
) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for x, y in points_abs:
        out.append(
            _localize_point(
                x=x,
                y=y,
                origin_x=origin_x,
                origin_y=origin_y,
                rotation_deg=rotation_deg,
            )
        )
    return out


def _localize_point(
    x: float,
    y: float,
    origin_x: float,
    origin_y: float,
    rotation_deg: float,
) -> tuple[float, float]:
    dx = float(x) - float(origin_x)
    dy = float(y) - float(origin_y)
    # Legacy Standard shape-string coordinates are authored in a Y-down frame.
    # We later normalize board coordinates with Y inversion. To recover a stable
    # package-local frame from LIB records across component rotations, local
    # point localization must use +rotation here (not -rotation).
    return _rotate_xy(dx, dy, rotation_deg)


def _pad_to_package_local(
    pad: dict[str, Any],
    origin_x: float,
    origin_y: float,
    rotation_deg: float,
) -> dict[str, Any]:
    px = _safe_float(pad.get("x"), 0.0)
    py = _safe_float(pad.get("y"), 0.0)
    dx = px - origin_x
    dy = py - origin_y
    local_x, local_y = _rotate_xy(dx, dy, rotation_deg)

    local = dict(pad)
    local["x"] = round(local_x, 6)
    local["y"] = round(local_y, 6)
    local_rotation = (_safe_float(pad.get("rotation"), 0.0) - rotation_deg) % 360.0
    local["rotation"] = round(local_rotation, 6)
    local["net"] = None
    return local


def _rotate_xy(x: float, y: float, rotation_deg: float) -> tuple[float, float]:
    angle = math.radians(float(rotation_deg or 0.0))
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)
    return (x * cos_a - y * sin_a, x * sin_a + y * cos_a)
