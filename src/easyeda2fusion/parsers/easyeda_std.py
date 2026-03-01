from __future__ import annotations

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
            # Legacy single-document Standard export. Heuristic: use declared docType.
            guessed = str(payload.get("docType", "schematic")).lower()
            doc_type = "board" if guessed in {"board", "pcb"} else "schematic"
            sink.append(
                ParsedDocument(
                    doc_type=doc_type,
                    name=str(payload.get("name", default_name)),
                    raw_objects=[o for o in payload["shape"] if isinstance(o, dict)],
                    metadata={k: v for k, v in payload.items() if k not in {"shape", "objects"}},
                )
            )

    @staticmethod
    def _object_list_from_doc(doc: dict[str, Any]) -> list[dict[str, Any]]:
        if isinstance(doc.get("objects"), list):
            return [item for item in doc["objects"] if isinstance(item, dict)]
        if isinstance(doc.get("shape"), list):
            return [item for item in doc["shape"] if isinstance(item, dict)]
        return []
