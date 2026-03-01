from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
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


class EasyEDAProParser(EasyEDAParser):
    source_format = SourceFormat.EASYEDA_PRO

    def can_parse(self, payload: dict[str, Any]) -> bool:
        if self._is_project_manifest(payload):
            return True

        fmt = str(payload.get("format", "")).lower()
        version = str(payload.get("editorVersion", "")).lower()
        family = str(payload.get("family", "")).lower()
        if fmt in {"easyeda_pro", "easyeda pro", "pro"}:
            return True
        if "pro" in version or "pro" in family:
            return True
        docs = payload.get("documents")
        if isinstance(docs, list):
            return any(
                str(doc.get("type", "")).lower() in {"schematic", "pcb", "board"}
                for doc in docs
                if isinstance(doc, dict)
            )
        return False

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
                        "PRO_NON_OBJECT_PAYLOAD",
                        f"Expected JSON object in {path.name}",
                        {"path": str(path)},
                    )
                )
                continue

            if not self.can_parse(payload):
                events.append(
                    project_event(
                        Severity.WARNING,
                        "PRO_FORMAT_UNCERTAIN",
                        f"{path.name} did not cleanly match Pro signature",
                        {"path": str(path)},
                    )
                )

            if self._is_project_manifest(payload):
                self._collect_docs_from_project_manifest(
                    payload=payload,
                    project_json_path=path,
                    sink_docs=documents,
                    sink_layers=layers,
                    sink_rules=rules,
                    sink_meta=metadata,
                    sink_events=events,
                )
                continue

            for key in ("meta", "settings", "project"):
                if isinstance(payload.get(key), dict):
                    metadata.update(payload[key])

            if "layers" in payload and isinstance(payload["layers"], list):
                layers.extend(item for item in payload["layers"] if isinstance(item, dict))

            if "rules" in payload and isinstance(payload["rules"], list):
                rules.extend(item for item in payload["rules"] if isinstance(item, dict))

            self._collect_docs_from_payload(payload, path.name, documents)

        if "coordinate_scale_to_mm" not in metadata and "unit" not in metadata:
            metadata["unit"] = "mm"
            events.append(
                project_event(
                    Severity.INFO,
                    "PRO_SCALE_DEFAULT_MM",
                    "No explicit coordinate scale found; defaulting to 1.0 mm scale for Pro",
                )
            )

        if not documents:
            events.append(
                project_event(
                    Severity.ERROR,
                    "PRO_NO_DOCUMENTS",
                    "No schematic or board documents were found in Pro input",
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
        docs = payload.get("documents")
        if isinstance(docs, list):
            for idx, doc in enumerate(docs):
                if not isinstance(doc, dict):
                    continue
                raw_type = str(doc.get("type", doc.get("docType", ""))).lower()
                doc_type = "board" if raw_type in {"pcb", "board", "layout"} else "schematic"
                sink.append(
                    ParsedDocument(
                        doc_type=doc_type,
                        name=str(doc.get("name", f"{doc_type}_{idx + 1}")),
                        raw_objects=self._object_list_from_doc(doc),
                        metadata={k: v for k, v in doc.items() if k not in {"objects", "items"}},
                    )
                )

        for key, doc_type in (("schematic", "schematic"), ("board", "board"), ("pcb", "board")):
            doc = payload.get(key)
            if isinstance(doc, dict):
                sink.append(
                    ParsedDocument(
                        doc_type=doc_type,
                        name=str(doc.get("name", f"{default_name}_{doc_type}")),
                        raw_objects=self._object_list_from_doc(doc),
                        metadata={k: v for k, v in doc.items() if k not in {"objects", "items"}},
                    )
                )

    @staticmethod
    def _object_list_from_doc(doc: dict[str, Any]) -> list[dict[str, Any]]:
        if isinstance(doc.get("objects"), list):
            return [item for item in doc["objects"] if isinstance(item, dict)]
        if isinstance(doc.get("items"), list):
            return [item for item in doc["items"] if isinstance(item, dict)]
        return []

    @staticmethod
    def _is_project_manifest(payload: dict[str, Any]) -> bool:
        return isinstance(payload.get("schematics"), dict) and isinstance(payload.get("pcbs"), dict)

    def _collect_docs_from_project_manifest(
        self,
        payload: dict[str, Any],
        project_json_path: Path,
        sink_docs: list[ParsedDocument],
        sink_layers: list[dict[str, Any]],
        sink_rules: list[dict[str, Any]],
        sink_meta: dict[str, Any],
        sink_events: list[Any],
    ) -> None:
        root = project_json_path.parent

        meta_path = root / "meta.json"
        if meta_path.exists():
            try:
                raw_meta = load_json(meta_path)
                if isinstance(raw_meta, dict):
                    sink_meta.update(raw_meta)
            except Exception as exc:
                sink_events.append(
                    project_event(
                        Severity.WARNING,
                        "PRO_META_READ_FAILED",
                        f"Unable to read meta.json: {exc}",
                        {"path": str(meta_path)},
                    )
                )

        sink_meta.setdefault("projectName", payload.get("projectName") or sink_meta.get("projectName") or root.name)
        sink_meta.setdefault("name", sink_meta.get("projectName") or root.name)

        # EasyEDA local project bundles using .esch/.epcb store geometry in mil-scale coordinates.
        sink_meta.setdefault("unit", "mil")
        sink_meta.setdefault("coordinate_scale_to_mm", 0.0254)
        primary_pcb_uuid = self._select_primary_pcb_uuid(payload)
        if primary_pcb_uuid:
            sink_meta["primary_pcb_uuid"] = primary_pcb_uuid

        footprint_meta = payload.get("footprints", {})
        device_meta = payload.get("devices", {})
        footprint_id_to_title = self._build_footprint_title_map(footprint_meta)
        device_id_to_footprint = self._build_device_id_to_footprint_map(
            device_meta,
            footprint_id_to_title,
        )
        device_title_to_footprint = self._build_device_title_to_footprint_map(
            device_meta,
            footprint_id_to_title,
        )
        designator_prefix_to_footprint = self._build_designator_prefix_to_footprint_map(
            device_meta,
            footprint_id_to_title,
        )

        sink_meta["footprint_id_to_title"] = footprint_id_to_title
        sink_meta["device_id_to_footprint"] = device_id_to_footprint
        sink_meta["device_title_to_footprint"] = device_title_to_footprint
        sink_meta["designator_prefix_to_footprint"] = designator_prefix_to_footprint
        device_id_to_symbol_id, device_name_to_symbol_id = self._build_device_symbol_maps(device_meta)
        sink_meta["device_id_to_symbol_id"] = device_id_to_symbol_id
        sink_meta["device_name_to_symbol_id"] = device_name_to_symbol_id
        sink_meta["symbol_defs"] = self._load_symbol_definitions(
            root=root,
            symbol_meta=payload.get("symbols", {}),
            sink_events=sink_events,
        )
        sink_meta["footprint_packages"] = self._load_footprint_packages(
            root=root,
            footprint_meta=footprint_meta,
            sink_events=sink_events,
        )

        schematics = payload.get("schematics", {})
        if isinstance(schematics, dict):
            for sch_uuid, sch_entry in schematics.items():
                if not isinstance(sch_entry, dict):
                    continue
                sch_name = str(sch_entry.get("name") or sch_uuid)
                sheets = sch_entry.get("sheets")
                if not isinstance(sheets, list):
                    continue

                for sheet in sheets:
                    if not isinstance(sheet, dict):
                        continue
                    sheet_name = str(sheet.get("name") or sheet.get("id") or "sheet")
                    sheet_id = str(sheet.get("id") or "1")
                    sheet_file = root / "SHEET" / str(sch_uuid) / f"{sheet_id}.esch"
                    if not sheet_file.exists():
                        sink_events.append(
                            project_event(
                                Severity.WARNING,
                                "PRO_SHEET_FILE_MISSING",
                                f"Schematic sheet file missing: {sheet_file.name}",
                                {"path": str(sheet_file), "schematic": sch_name},
                            )
                        )
                        continue

                    records = self._load_line_records(sheet_file, sink_events)
                    objects = self._convert_esch_records(records)
                    sink_docs.append(
                        ParsedDocument(
                            doc_type="schematic",
                            name=f"{sch_name}:{sheet_name}",
                            raw_objects=objects,
                            metadata={
                                "source_file": str(sheet_file),
                                "schematic_uuid": str(sch_uuid),
                                "sheet_id": sheet_id,
                            },
                        )
                    )

        pcbs = payload.get("pcbs", {})
        if isinstance(pcbs, dict):
            for pcb_uuid, pcb_name in pcbs.items():
                if primary_pcb_uuid and str(pcb_uuid) != primary_pcb_uuid:
                    sink_events.append(
                        project_event(
                            Severity.INFO,
                            "PRO_PCB_SKIPPED_NON_PRIMARY",
                            "Skipping non-primary PCB from project manifest",
                            {
                                "pcb_uuid": str(pcb_uuid),
                                "pcb_name": str(pcb_name),
                                "primary_pcb_uuid": primary_pcb_uuid,
                            },
                        )
                    )
                    continue
                pcb_file = root / "PCB" / f"{pcb_uuid}.epcb"
                if not pcb_file.exists():
                    sink_events.append(
                        project_event(
                            Severity.WARNING,
                            "PRO_PCB_FILE_MISSING",
                            f"PCB file missing: {pcb_file.name}",
                            {"path": str(pcb_file)},
                        )
                    )
                    continue

                records = self._load_line_records(pcb_file, sink_events)
                doc_layers, doc_rules, objects, canvas_meta = self._convert_epcb_records(
                    records,
                    footprint_id_to_title=footprint_id_to_title,
                    device_id_to_footprint=device_id_to_footprint,
                    device_title_to_footprint=device_title_to_footprint,
                    designator_prefix_to_footprint=designator_prefix_to_footprint,
                )
                sink_layers.extend(doc_layers)
                sink_rules.extend(doc_rules)
                sink_meta.update({k: v for k, v in canvas_meta.items() if v is not None})
                sink_docs.append(
                    ParsedDocument(
                        doc_type="board",
                        name=str(pcb_name),
                        raw_objects=objects,
                        metadata={"source_file": str(pcb_file), "pcb_uuid": str(pcb_uuid)},
                    )
                )

    @staticmethod
    def _select_primary_pcb_uuid(payload: dict[str, Any]) -> str | None:
        boards = payload.get("boards")
        if isinstance(boards, dict):
            for _, board_entry in boards.items():
                if not isinstance(board_entry, dict):
                    continue
                pcb_uuid = str(board_entry.get("pcb") or "").strip()
                if pcb_uuid:
                    return pcb_uuid
        return None

    @staticmethod
    def _load_line_records(path: Path, sink_events: list[Any]) -> list[list[Any]]:
        records: list[list[Any]] = []
        with path.open("r", encoding="utf-8") as f:
            for line_no, raw_line in enumerate(f, start=1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except Exception:
                    sink_events.append(
                        project_event(
                            Severity.WARNING,
                            "PRO_RECORD_PARSE_FAILED",
                            f"Skipping non-JSON line {line_no} in {path.name}",
                            {"path": str(path), "line_no": line_no},
                        )
                    )
                    continue
                if isinstance(parsed, list) and parsed:
                    records.append(parsed)
        return records

    @staticmethod
    def _build_footprint_title_map(footprint_meta: Any) -> dict[str, str]:
        mapping: dict[str, str] = {}
        if not isinstance(footprint_meta, dict):
            return mapping

        for fp_id, fp_info in footprint_meta.items():
            if not isinstance(fp_id, str):
                continue
            title = fp_id
            if isinstance(fp_info, dict) and fp_info.get("title"):
                title = str(fp_info.get("title"))
            mapping[fp_id] = title
        return mapping

    @staticmethod
    def _build_device_title_to_footprint_map(
        device_meta: Any,
        footprint_id_to_title: dict[str, str],
    ) -> dict[str, str]:
        mapping: dict[str, str] = {}
        if not isinstance(device_meta, dict):
            return mapping

        for _, device in device_meta.items():
            if not isinstance(device, dict):
                continue
            attrs = device.get("attributes")
            if not isinstance(attrs, dict):
                attrs = {}

            fp_id = str(attrs.get("Footprint") or "").strip()
            if not fp_id:
                continue
            fp_title = footprint_id_to_title.get(fp_id, fp_id)

            for raw_name in (
                device.get("title"),
                attrs.get("Name"),
                attrs.get("Device"),
            ):
                name = str(raw_name or "").strip()
                if name:
                    mapping[_canon_key(name)] = fp_title
        return mapping

    @staticmethod
    def _build_device_id_to_footprint_map(
        device_meta: Any,
        footprint_id_to_title: dict[str, str],
    ) -> dict[str, str]:
        mapping: dict[str, str] = {}
        if not isinstance(device_meta, dict):
            return mapping

        for device_id, device in device_meta.items():
            key = str(device_id or "").strip()
            if not key:
                continue
            if not isinstance(device, dict):
                continue
            attrs = device.get("attributes")
            if not isinstance(attrs, dict):
                attrs = {}
            fp_id = str(attrs.get("Footprint") or "").strip()
            if not fp_id:
                continue
            mapping[key] = footprint_id_to_title.get(fp_id, fp_id)
        return mapping

    @staticmethod
    def _build_designator_prefix_to_footprint_map(
        device_meta: Any,
        footprint_id_to_title: dict[str, str],
    ) -> dict[str, str]:
        if not isinstance(device_meta, dict):
            return {}

        counts: dict[str, Counter[str]] = defaultdict(Counter)
        for _, device in device_meta.items():
            if not isinstance(device, dict):
                continue
            attrs = device.get("attributes")
            if not isinstance(attrs, dict):
                attrs = {}

            designator = str(attrs.get("Designator") or "").strip()
            prefix = _refdes_prefix(designator)
            if not prefix:
                continue

            fp_id = str(attrs.get("Footprint") or "").strip()
            if not fp_id:
                continue

            fp_title = footprint_id_to_title.get(fp_id, fp_id)
            counts[prefix][fp_title] += 1

        winner_map: dict[str, str] = {}
        for prefix, counter in counts.items():
            if not counter:
                continue
            winner_map[prefix] = counter.most_common(1)[0][0]
        return winner_map

    @staticmethod
    def _build_device_symbol_maps(device_meta: Any) -> tuple[dict[str, str], dict[str, str]]:
        by_id: dict[str, str] = {}
        by_name: dict[str, str] = {}
        if not isinstance(device_meta, dict):
            return by_id, by_name

        for device_id, device in device_meta.items():
            if not isinstance(device, dict):
                continue
            attrs = device.get("attributes")
            if not isinstance(attrs, dict):
                attrs = {}

            symbol_id = str(attrs.get("Symbol") or "").strip()
            if not symbol_id:
                continue

            id_key = str(device_id or "").strip()
            if id_key:
                by_id[id_key] = symbol_id

            for raw_name in (
                device.get("title"),
                attrs.get("Name"),
                attrs.get("Device"),
                attrs.get("sourceId"),
            ):
                name = str(raw_name or "").strip()
                if not name:
                    continue
                by_name[_canon_key(name)] = symbol_id

        return by_id, by_name

    def _load_symbol_definitions(
        self,
        root: Path,
        symbol_meta: Any,
        sink_events: list[Any],
    ) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        if not isinstance(symbol_meta, dict):
            return out

        for symbol_id, symbol_info in symbol_meta.items():
            if not isinstance(symbol_id, str):
                continue
            symbol_file = root / "SYMBOL" / f"{symbol_id}.esym"
            if not symbol_file.exists():
                sink_events.append(
                    project_event(
                        Severity.WARNING,
                        "PRO_SYMBOL_FILE_MISSING",
                        f"Symbol file missing: {symbol_file.name}",
                        {"path": str(symbol_file)},
                    )
                )
                continue

            symbol_title = symbol_id
            if isinstance(symbol_info, dict) and symbol_info.get("title"):
                symbol_title = str(symbol_info.get("title"))

            records = self._load_line_records(symbol_file, sink_events)
            symbol_def = self._convert_esym_records_to_symbol_def(
                symbol_id=symbol_id,
                title=symbol_title,
                records=records,
            )
            if symbol_def is not None and symbol_def.get("pins"):
                out[symbol_id] = symbol_def
        return out

    def _load_footprint_packages(
        self,
        root: Path,
        footprint_meta: Any,
        sink_events: list[Any],
    ) -> list[dict[str, Any]]:
        packages: list[dict[str, Any]] = []
        if not isinstance(footprint_meta, dict):
            return packages

        for fp_id, fp_info in footprint_meta.items():
            if not isinstance(fp_id, str):
                continue
            fp_file = root / "FOOTPRINT" / f"{fp_id}.efoo"
            if not fp_file.exists():
                sink_events.append(
                    project_event(
                        Severity.WARNING,
                        "PRO_FOOTPRINT_FILE_MISSING",
                        f"Footprint file missing: {fp_file.name}",
                        {"path": str(fp_file)},
                    )
                )
                continue

            title = fp_id
            if isinstance(fp_info, dict) and fp_info.get("title"):
                title = str(fp_info.get("title"))

            records = self._load_line_records(fp_file, sink_events)
            package = self._convert_efoo_records_to_package(fp_id, title, records)
            if package is not None:
                packages.append(package)

        return packages

    @staticmethod
    def _convert_esch_records(records: list[list[Any]]) -> list[dict[str, Any]]:
        objects: list[dict[str, Any]] = []

        for record in records:
            token = str(record[0]).upper()

            if token == "COMPONENT":
                attrs = record[7] if len(record) > 7 and isinstance(record[7], dict) else {}
                refdes = str(attrs.get("Designator") or attrs.get("Ref") or "").strip()
                source_name = str(attrs.get("Name") or attrs.get("Device") or "").strip()
                if not refdes and not source_name:
                    continue
                objects.append(
                    {
                        "type": "component",
                        "id": record[1] if len(record) > 1 else None,
                        "refdes": refdes or str(record[1] or "U?"),
                        "value": str(attrs.get("Value") or ""),
                        "source_name": source_name or str(record[1] or "component"),
                        "x": _safe_float(record[4] if len(record) > 4 else 0.0),
                        "y": _safe_float(record[5] if len(record) > 5 else 0.0),
                        "rotation": _safe_float(record[6] if len(record) > 6 else 0.0),
                        "attributes": attrs,
                    }
                )
                continue

            if token in {"ATTR", "STRING"}:
                if token == "ATTR":
                    text = str(record[4]) if len(record) > 4 else ""
                    x = _safe_float(record[7] if len(record) > 7 else 0.0)
                    y = _safe_float(record[8] if len(record) > 8 else 0.0)
                    rot = _safe_float(record[9] if len(record) > 9 else 0.0)
                else:
                    text = str(record[6]) if len(record) > 6 else ""
                    x = _safe_float(record[4] if len(record) > 4 else 0.0)
                    y = _safe_float(record[5] if len(record) > 5 else 0.0)
                    rot = _safe_float(record[10] if len(record) > 10 else 0.0)

                objects.append(
                    {
                        "type": "text",
                        "id": record[1] if len(record) > 1 else None,
                        "text": text,
                        "x": x,
                        "y": y,
                        "rotation": rot,
                    }
                )

        return objects

    @staticmethod
    def _convert_epcb_records(
        records: list[list[Any]],
        footprint_id_to_title: dict[str, str] | None = None,
        device_id_to_footprint: dict[str, str] | None = None,
        device_title_to_footprint: dict[str, str] | None = None,
        designator_prefix_to_footprint: dict[str, str] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
        footprint_id_to_title = footprint_id_to_title or {}
        device_id_to_footprint = device_id_to_footprint or {}
        device_title_to_footprint = device_title_to_footprint or {}
        designator_prefix_to_footprint = designator_prefix_to_footprint or {}

        layers: list[dict[str, Any]] = []
        rules: list[dict[str, Any]] = []
        objects: list[dict[str, Any]] = []
        canvas_meta: dict[str, Any] = {}

        component_inline_attrs: dict[str, dict[str, Any]] = {}
        component_attr_map: dict[str, dict[str, Any]] = defaultdict(dict)

        for record in records:
            token = str(record[0]).upper()
            if token == "COMPONENT":
                comp_id = str(record[1]) if len(record) > 1 else ""
                if not comp_id:
                    continue
                attrs = record[7] if len(record) > 7 and isinstance(record[7], dict) else {}
                component_inline_attrs[comp_id] = dict(attrs)
            elif token == "ATTR":
                parent_id = str(record[3]) if len(record) > 3 else ""
                key = str(record[7]) if len(record) > 7 else ""
                if not parent_id or not key:
                    continue
                value = record[8] if len(record) > 8 else ""
                component_attr_map[parent_id][key] = value

        component_refdes_by_id: dict[str, str] = {}
        for comp_id, inline_attrs in component_inline_attrs.items():
            merged = {**inline_attrs, **component_attr_map.get(comp_id, {})}
            refdes = str(merged.get("Designator") or merged.get("Ref") or "").strip()
            if refdes:
                component_refdes_by_id[comp_id] = refdes

        # POURED records reference their parent POUR id. Capture net/layer from the source POUR
        # so we do not treat POURED record scalar fields (clearance/flags) as net names.
        pour_meta_by_id: dict[str, dict[str, Any]] = {}
        poured_parent_ids: set[str] = set()
        for record in records:
            token = str(record[0]).upper()
            if token != "POUR":
                if token == "POURED":
                    if len(record) > 4 and bool(record[4]):
                        parent_pour_id = str(record[2]) if len(record) > 2 else ""
                        if parent_pour_id:
                            poured_parent_ids.add(parent_pour_id)
                continue
            pour_id = str(record[1]) if len(record) > 1 else ""
            if not pour_id:
                continue
            layer_idx = int(record[4]) if len(record) > 4 and isinstance(record[4], (int, float)) else 1
            net_name = str(record[3]) if len(record) > 3 else ""
            pour_meta_by_id[pour_id] = {
                "layer": str(layer_idx),
                "net": net_name,
            }

        for record in records:
            token = str(record[0]).upper()

            if token == "CANVAS":
                canvas_meta["display_unit"] = record[3] if len(record) > 3 else None
                continue

            if token == "LAYER":
                layers.append(
                    {
                        "id": str(record[1]) if len(record) > 1 else "",
                        "name": str(record[3]) if len(record) > 3 else str(record[2] if len(record) > 2 else ""),
                    }
                )
                continue

            if token == "RULE":
                rules.append(
                    {
                        "name": str(record[2]) if len(record) > 2 else "rule",
                        "value": json.dumps(record[4]) if len(record) > 4 else "",
                        "description": f"rule_type_{record[1]}" if len(record) > 1 else None,
                    }
                )
                continue

            if token == "NET":
                objects.append({"type": "net", "name": str(record[1]) if len(record) > 1 else "N$UNNAMED"})
                continue

            if token == "COMPONENT":
                comp_id = str(record[1]) if len(record) > 1 else ""
                attrs = dict(component_inline_attrs.get(comp_id, {}))
                attrs.update(component_attr_map.get(comp_id, {}))
                refdes = str(attrs.get("Designator") or attrs.get("Ref") or "").strip()
                source_name = str(attrs.get("Name") or attrs.get("Device") or "").strip()
                if not refdes and not source_name:
                    continue
                if not refdes:
                    # Skip anonymous component internals; keep only resolvable placed parts.
                    continue

                package = _infer_component_package(
                    attrs=attrs,
                    refdes=refdes,
                    footprint_id_to_title=footprint_id_to_title,
                    device_id_to_footprint=device_id_to_footprint,
                    device_title_to_footprint=device_title_to_footprint,
                    designator_prefix_to_footprint=designator_prefix_to_footprint,
                )
                objects.append(
                    {
                        "type": "component",
                        "id": comp_id or None,
                        "refdes": refdes,
                        "value": str(attrs.get("Value") or source_name or ""),
                        "source_name": source_name or refdes,
                        "x": _safe_float(record[4] if len(record) > 4 else 0.0),
                        "y": _safe_float(record[5] if len(record) > 5 else 0.0),
                        "rotation": _safe_float(record[6] if len(record) > 6 else 0.0),
                        "side": "bottom" if str(record[3] if len(record) > 3 else "1") == "2" else "top",
                        "attributes": attrs,
                        "package": package,
                        "mpn": str(attrs.get("Manufacturer Part") or ""),
                    }
                )
                continue

            if token == "PAD_NET":
                comp_id = str(record[1]) if len(record) > 1 else ""
                pin = str(record[2]) if len(record) > 2 else ""
                net_name = str(record[3]) if len(record) > 3 else ""
                if not pin or not net_name:
                    continue
                refdes = component_refdes_by_id.get(comp_id, comp_id or "UNRESOLVED")
                objects.append(
                    {
                        "type": "net",
                        "name": net_name,
                        "nodes": [{"refdes": refdes, "pin": pin}],
                    }
                )
                continue

            if token == "LINE":
                layer_text = str(record[4]) if len(record) > 4 else ""
                net_name = str(record[3]) if len(record) > 3 else ""
                obj_type = "mechanical" if layer_text == "11" else "track"
                payload = {
                    "type": obj_type,
                    "id": record[1] if len(record) > 1 else None,
                    "net": net_name,
                    "layer": layer_text,
                    "x1": _safe_float(record[5] if len(record) > 5 else 0.0),
                    "y1": _safe_float(record[6] if len(record) > 6 else 0.0),
                    "x2": _safe_float(record[7] if len(record) > 7 else 0.0),
                    "y2": _safe_float(record[8] if len(record) > 8 else 0.0),
                    "width": _safe_float(record[9] if len(record) > 9 else 6.0),
                }
                objects.append(payload)
                continue

            if token == "VIA":
                objects.append(
                    {
                        "type": "via",
                        "id": record[1] if len(record) > 1 else None,
                        "net": str(record[3]) if len(record) > 3 else None,
                        "x": _safe_float(record[5] if len(record) > 5 else 0.0),
                        "y": _safe_float(record[6] if len(record) > 6 else 0.0),
                        "drill": _safe_float(record[7] if len(record) > 7 else 12.0),
                        "diameter": _safe_float(record[8] if len(record) > 8 else 24.0),
                    }
                )
                continue

            if token == "PAD":
                drill_shape = record[9] if len(record) > 9 and isinstance(record[9], list) else []
                copper_shape = record[10] if len(record) > 10 and isinstance(record[10], list) else []
                width, height, drill, shape_name = EasyEDAProParser._pad_geometry_from_shapes(
                    drill_shape=drill_shape,
                    copper_shape=copper_shape,
                )
                objects.append(
                    {
                        "type": "pad",
                        "id": record[1] if len(record) > 1 else None,
                        "net": str(record[3]) if len(record) > 3 else None,
                        "layer": str(record[4]) if len(record) > 4 else "1",
                        "name": str(record[5]) if len(record) > 5 else "",
                        "x": _safe_float(record[6] if len(record) > 6 else 0.0),
                        "y": _safe_float(record[7] if len(record) > 7 else 0.0),
                        "rotation": _safe_float(record[8] if len(record) > 8 else 0.0),
                        "width": width,
                        "height": height,
                        "drill": drill,
                        "shape": shape_name,
                    }
                )
                continue

            if token in {"POLY", "POUR", "POURED"}:
                layer_idx = 11
                net_name = ""
                points_source: Any = []
                is_outline = False

                if token == "POLY":
                    layer_idx = int(record[4]) if len(record) > 4 and isinstance(record[4], (int, float)) else 12
                    net_name = str(record[3]) if len(record) > 3 else ""
                    points_source = record[6] if len(record) > 6 else []
                    is_outline = layer_idx == 11
                elif token == "POUR":
                    pour_id = str(record[1]) if len(record) > 1 else ""
                    # When explicit POURED geometry exists, replay that generated contour only.
                    # Raw POUR records frequently flatten holes/rings into noisy paths.
                    if pour_id and pour_id in poured_parent_ids:
                        continue
                    layer_idx = int(record[4]) if len(record) > 4 and isinstance(record[4], (int, float)) else 1
                    net_name = str(record[3]) if len(record) > 3 else ""
                    points_source = record[8] if len(record) > 8 else []
                else:
                    # POURED is generated geometry derived from a parent POUR record.
                    # record[4] indicates a valid closed poured shape; false rows are
                    # small edge fragments that should not become standalone regions.
                    if len(record) > 4 and not bool(record[4]):
                        continue
                    parent_pour_id = str(record[2]) if len(record) > 2 else ""
                    parent_meta = pour_meta_by_id.get(parent_pour_id, {})
                    layer_idx = int(parent_meta.get("layer", "1"))
                    net_name = str(parent_meta.get("net") or "")
                    points_source = record[5] if len(record) > 5 else []
                    if EasyEDAProParser._is_complex_poured_geometry(points_source):
                        continue

                points = EasyEDAProParser._extract_point_list(
                    points_source,
                    primary_ring_only=token == "POURED",
                )
                if not points:
                    continue

                objects.append(
                    {
                        "type": "outline" if is_outline else "region",
                        "id": record[1] if len(record) > 1 else None,
                        "layer": str(layer_idx),
                        "net": net_name,
                        "points": points,
                    }
                )
                continue

            if token == "FILL":
                layer_idx = int(record[4]) if len(record) > 4 and isinstance(record[4], (int, float)) else 13
                points = EasyEDAProParser._extract_point_list(record[7] if len(record) > 7 else [])
                if not points:
                    continue
                obj_type = "outline" if layer_idx == 11 else "region"
                objects.append(
                    {
                        "type": obj_type,
                        "id": record[1] if len(record) > 1 else None,
                        "layer": str(layer_idx),
                        "net": str(record[3]) if len(record) > 3 else "",
                        "points": points,
                    }
                )
                continue

            if token in {"STRING", "ATTR"}:
                if token == "STRING":
                    text = str(record[6]) if len(record) > 6 else ""
                    x = _safe_float(record[4] if len(record) > 4 else 0.0)
                    y = _safe_float(record[5] if len(record) > 5 else 0.0)
                    layer = str(record[3]) if len(record) > 3 else "3"
                    size = _safe_float(record[8] if len(record) > 8 else 39.37)
                    rotation = EasyEDAProParser._string_rotation(record)
                else:
                    parent_id = str(record[3]) if len(record) > 3 else ""
                    if parent_id in component_inline_attrs:
                        # Component metadata ATTRs are already merged into component attributes.
                        continue
                    key = str(record[7]) if len(record) > 7 else ""
                    val = str(record[8]) if len(record) > 8 else ""
                    text = f"{key}={val}" if key else val
                    x = _safe_float(record[5] if len(record) > 5 else 0.0)
                    y = _safe_float(record[6] if len(record) > 6 else 0.0)
                    layer = str(record[4]) if len(record) > 4 else "3"
                    size = _safe_float(record[12] if len(record) > 12 else 20.0)
                    rotation = _safe_float(record[9] if len(record) > 9 else 0.0)

                objects.append(
                    {
                        "type": "text",
                        "id": record[1] if len(record) > 1 else None,
                        "layer": layer,
                        "x": x,
                        "y": y,
                        "text": text,
                        "size": size,
                        "rotation": rotation,
                    }
                )

        return layers, rules, objects, canvas_meta

    @staticmethod
    def _convert_efoo_records_to_package(
        footprint_id: str,
        title: str,
        records: list[list[Any]],
    ) -> dict[str, Any] | None:
        pads: list[dict[str, Any]] = []
        outline_items: list[dict[str, Any]] = []

        for record in records:
            token = str(record[0]).upper()
            if token == "PAD":
                drill_shape = record[9] if len(record) > 9 and isinstance(record[9], list) else []
                copper_shape = record[10] if len(record) > 10 and isinstance(record[10], list) else []
                pad_name = str(record[5]) if len(record) > 5 else ""
                if not pad_name:
                    continue

                width, height, drill_val, shape_name = EasyEDAProParser._pad_geometry_from_shapes(
                    drill_shape=drill_shape,
                    copper_shape=copper_shape,
                )
                pads.append(
                    {
                        "name": pad_name,
                        "x": _safe_float(record[6] if len(record) > 6 else 0.0),
                        "y": _safe_float(record[7] if len(record) > 7 else 0.0),
                        "rotation": _safe_float(record[8] if len(record) > 8 else 0.0),
                        "width": width,
                        "height": height,
                        "drill": drill_val,
                        "shape": shape_name,
                        "layer": str(record[4]) if len(record) > 4 else "1",
                    }
                )
                continue

            if token in {"POLY", "FILL"}:
                layer = str(record[4]) if len(record) > 4 else ""
                points_source = record[6] if token == "POLY" and len(record) > 6 else record[7] if len(record) > 7 else []
                points = EasyEDAProParser._extract_point_list(points_source)
                if len(points) >= 2:
                    outline_items.append(
                        {
                            "kind": "wire_path",
                            "layer": layer,
                            "width": _safe_float(record[5] if len(record) > 5 else 0.2),
                            "points": points,
                        }
                    )
                continue

            if token == "STRING":
                layer = str(record[3]) if len(record) > 3 else ""
                text = str(record[6]) if len(record) > 6 else ""
                if text:
                    outline_items.append(
                        {
                            "kind": "text",
                            "layer": layer,
                            "text": text,
                            "x": _safe_float(record[4] if len(record) > 4 else 0.0),
                            "y": _safe_float(record[5] if len(record) > 5 else 0.0),
                            "size": _safe_float(record[8] if len(record) > 8 else 39.37),
                            "rotation": EasyEDAProParser._string_rotation(record),
                        }
                    )
                continue

            if token == "ATTR":
                layer = str(record[4]) if len(record) > 4 else ""
                key = str(record[7]) if len(record) > 7 else ""
                value = str(record[8]) if len(record) > 8 else ""
                normalized = key.strip().lower()
                if normalized == "designator":
                    text = ">NAME"
                elif normalized == "value":
                    text = ">VALUE"
                else:
                    text = value if value else ""
                if text:
                    outline_items.append(
                        {
                            "kind": "text",
                            "layer": layer,
                            "text": text,
                            "x": _safe_float(record[5] if len(record) > 5 else 0.0),
                            "y": _safe_float(record[6] if len(record) > 6 else 0.0),
                            "size": _safe_float(record[12] if len(record) > 12 else 20.0),
                            "rotation": _safe_float(record[9] if len(record) > 9 else 0.0),
                        }
                    )
                continue

        if not pads:
            return None

        return {
            "type": "package",
            "id": footprint_id,
            "name": title or footprint_id,
            "pads": pads,
            "outline": outline_items,
        }

    @staticmethod
    def _convert_esym_records_to_symbol_def(
        symbol_id: str,
        title: str,
        records: list[list[Any]],
    ) -> dict[str, Any] | None:
        pins_by_id: dict[str, dict[str, Any]] = {}

        for record in records:
            token = str(record[0]).upper()
            if token != "PIN":
                continue
            pin_id = str(record[1]) if len(record) > 1 else ""
            if not pin_id:
                continue
            pins_by_id[pin_id] = {
                "pin_id": pin_id,
                "number": "",
                "name": "",
                "pin_type": "",
                "x": _safe_float(record[4] if len(record) > 4 else 0.0),
                "y": _safe_float(record[5] if len(record) > 5 else 0.0),
                "rotation": _safe_float(record[7] if len(record) > 7 else 0.0),
            }

        if not pins_by_id:
            return None

        for record in records:
            token = str(record[0]).upper()
            if token != "ATTR":
                continue
            parent_pin_id = str(record[2]) if len(record) > 2 else ""
            if parent_pin_id not in pins_by_id:
                continue
            key = str(record[3]).strip().lower() if len(record) > 3 else ""
            value = str(record[4]).strip() if len(record) > 4 else ""
            if not key:
                continue
            if key == "number":
                pins_by_id[parent_pin_id]["number"] = value
            elif key == "name":
                pins_by_id[parent_pin_id]["name"] = value
            elif key in {"pin type", "pintype", "type"}:
                pins_by_id[parent_pin_id]["pin_type"] = value

        pins: list[dict[str, Any]] = []
        for pin in pins_by_id.values():
            number = str(pin.get("number") or "").strip()
            name = str(pin.get("name") or "").strip()
            if not number:
                number = name
            if not number:
                continue
            pins.append(
                {
                    "number": number,
                    "name": name or number,
                    "pin_type": str(pin.get("pin_type") or "").strip(),
                    "x": float(pin.get("x", 0.0)),
                    "y": float(pin.get("y", 0.0)),
                    "rotation": float(pin.get("rotation", 0.0)),
                }
            )

        if not pins:
            return None

        pins.sort(key=lambda item: _symbol_pin_sort_key(str(item.get("number") or "")))
        return {
            "id": symbol_id,
            "name": title or symbol_id,
            "pins": pins,
        }

    @staticmethod
    def _extract_point_list(raw: Any, primary_ring_only: bool = False) -> list[list[float]]:
        if not isinstance(raw, list):
            return []

        # Flat command stream: [x, y, 'L', x, y, ...]
        if raw and isinstance(raw[0], (int, float, str)):
            if str(raw[0]).upper() == "CIRCLE":
                return EasyEDAProParser._circle_points_from_command(raw)
            return [[x, y] for x, y in EasyEDAProParser._points_from_command_stream(raw)]

        rings: list[list[list[float]]] = []
        for ring in raw:
            if isinstance(ring, list):
                if ring and str(ring[0]).upper() == "CIRCLE":
                    circle_points = EasyEDAProParser._circle_points_from_command(ring)
                    if circle_points:
                        rings.append(circle_points)
                    continue
                ring_points: list[list[float]] = []
                for x, y in EasyEDAProParser._points_from_command_stream(ring):
                    ring_points.append([x, y])
                if ring_points:
                    rings.append(ring_points)

        if not rings:
            return []
        if primary_ring_only:
            return rings[0]

        points: list[list[float]] = []
        for ring_points in rings:
            points.extend(ring_points)
        return points

    @staticmethod
    def _circle_points_from_command(stream: list[Any], segments: int = 24) -> list[list[float]]:
        if len(stream) < 4:
            return []
        try:
            cx = float(stream[1])
            cy = float(stream[2])
            radius = abs(float(stream[3]))
        except Exception:
            return []
        if radius <= 0.0:
            return []
        points: list[list[float]] = []
        for idx in range(segments):
            angle = (idx / segments) * 2.0 * 3.141592653589793
            points.append([cx + radius * math.cos(angle), cy + radius * math.sin(angle)])
        return points

    @staticmethod
    def _pad_geometry_from_shapes(
        drill_shape: list[Any],
        copper_shape: list[Any],
    ) -> tuple[float, float, float | None, str]:
        shape_source = copper_shape if copper_shape else drill_shape
        shape_name = str(shape_source[0]).lower() if len(shape_source) > 0 else "rect"

        width = 0.0
        height = 0.0
        if shape_name == "poly" and len(shape_source) > 1 and isinstance(shape_source[1], list):
            points = EasyEDAProParser._extract_point_list(shape_source[1])
            if points:
                xs = [float(point[0]) for point in points]
                ys = [float(point[1]) for point in points]
                width = abs(max(xs) - min(xs))
                height = abs(max(ys) - min(ys))
        else:
            width = _safe_float(shape_source[1] if len(shape_source) > 1 else 40.0)
            height = _safe_float(shape_source[2] if len(shape_source) > 2 else width)

        if width <= 0.0:
            width = 40.0
        if height <= 0.0:
            height = width

        drill: float | None = None
        if drill_shape:
            drill_candidate = _safe_float(drill_shape[1] if len(drill_shape) > 1 else 0.0)
            if drill_candidate > 0.0:
                drill = drill_candidate
        return width, height, drill, shape_name

    @staticmethod
    def _string_rotation(record: list[Any]) -> float:
        # EasyEDA Pro STRING records in .epcb/.efoo store rotation at index 13.
        # Keep index 10 as fallback for compatibility with older variants.
        if len(record) > 13:
            return _safe_float(record[13])
        if len(record) > 10:
            return _safe_float(record[10])
        return 0.0

    @staticmethod
    def _points_from_command_stream(stream: list[Any]) -> list[tuple[float, float]]:
        points: list[tuple[float, float]] = []
        idx = 0
        skip_next_numeric = False
        numeric_enabled = True
        while idx < len(stream):
            token = stream[idx]
            if isinstance(token, str):
                cmd = token.upper()
                if cmd == "ARC":
                    # Arc command format includes an angle parameter before endpoint coordinates.
                    skip_next_numeric = True
                    numeric_enabled = True
                elif cmd == "L":
                    numeric_enabled = True
                else:
                    numeric_enabled = False
                idx += 1
                continue

            if not isinstance(token, (int, float)):
                idx += 1
                continue

            if not numeric_enabled:
                idx += 1
                continue

            if skip_next_numeric:
                skip_next_numeric = False
                idx += 1
                continue

            if idx + 1 >= len(stream):
                break
            next_token = stream[idx + 1]
            if not isinstance(next_token, (int, float)):
                idx += 1
                continue
            points.append((float(token), float(next_token)))
            idx += 2
        return points

    @staticmethod
    def _is_complex_poured_geometry(raw: Any) -> bool:
        if not isinstance(raw, list):
            return False

        first_ring: Any = raw
        if raw and isinstance(raw[0], list):
            if len(raw) > 1:
                return True
            first_ring = raw[0]

        if not isinstance(first_ring, list):
            return False

        if len(first_ring) > 120:
            return True

        arc_count = sum(
            1 for item in first_ring if isinstance(item, str) and item.upper() == "ARC"
        )
        return arc_count > 8


def _safe_float(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except Exception:
        return 0.0


def _canon_key(value: Any) -> str:
    text = str(value or "").strip().upper()
    return re.sub(r"[^A-Z0-9]+", "", text)


def _refdes_prefix(refdes: Any) -> str:
    text = str(refdes or "").strip().upper()
    match = re.match(r"^([A-Z]+)", text)
    return match.group(1) if match else ""


def _extract_package_hint(value: Any) -> str:
    text = str(value or "").upper()
    if not text:
        return ""

    direct_codes = ("0201", "0402", "0603", "0805", "1206", "1210", "2512")
    for code in direct_codes:
        if code in text:
            return code

    patterns = [
        r"(SOT[-_ ]?23(?:[-_ ]?\d+)?)",
        r"(SOT[-_ ]?223)",
        r"(SOT[-_ ]?89)",
        r"(SOIC[-_ ]?\d+)",
        r"(TSSOP[-_ ]?\d+)",
        r"(QFN[-_ ]?\d+)",
        r"(QFP[-_ ]?\d+)",
        r"(LQFP[-_ ]?\d+)",
        r"(DIP[-_ ]?\d+)",
        r"(SMA)",
        r"(SMB)",
        r"(SMC)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1).replace("_", "-").replace(" ", "")

    return ""


def _symbol_pin_sort_key(value: str) -> tuple[int, int, str]:
    token = str(value or "").strip()
    if token.isdigit():
        return (0, int(token), "")
    match = re.match(r"^([A-Za-z]+)(\d+)$", token)
    if match:
        return (1, int(match.group(2)), match.group(1))
    return (2, 0, token)


def _infer_component_package(
    attrs: dict[str, Any],
    refdes: str,
    footprint_id_to_title: dict[str, str],
    device_id_to_footprint: dict[str, str],
    device_title_to_footprint: dict[str, str],
    designator_prefix_to_footprint: dict[str, str],
) -> str:
    raw_footprint = str(attrs.get("Footprint") or attrs.get("Package") or "").strip()
    if raw_footprint and raw_footprint.lower() not in {"none", "null"}:
        return footprint_id_to_title.get(raw_footprint, raw_footprint)

    raw_device_id = str(attrs.get("Device") or attrs.get("device_id") or "").strip()
    if raw_device_id:
        mapped = device_id_to_footprint.get(raw_device_id)
        if mapped:
            return mapped

    for key in (
        attrs.get("Name"),
        attrs.get("Device"),
        attrs.get("sourceId"),
    ):
        name = str(key or "").strip()
        if not name:
            continue
        mapped = device_title_to_footprint.get(_canon_key(name))
        if mapped:
            return mapped

    prefix = _refdes_prefix(refdes)
    if prefix and prefix not in {"R", "C", "L", "D", "FB"}:
        mapped = designator_prefix_to_footprint.get(prefix)
        if mapped:
            return mapped

    for key in (
        attrs.get("3D Model Title"),
        attrs.get("Name"),
        attrs.get("Manufacturer Part"),
        attrs.get("Value"),
    ):
        hint = _extract_package_hint(key)
        if hint:
            return hint

    return ""
