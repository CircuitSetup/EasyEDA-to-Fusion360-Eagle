from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from easyeda2fusion.model import Layer, Severity, SourceFormat, project_event


STD_LAYER_BY_ID: dict[str, tuple[str, str, int | None]] = {
    "1": ("top_copper", "electrical", 1),
    "2": ("bottom_copper", "electrical", 2),
    "3": ("top_silkscreen", "silkscreen", None),
    "4": ("bottom_silkscreen", "silkscreen", None),
    "5": ("top_paste", "paste", None),
    "6": ("bottom_paste", "paste", None),
    "7": ("top_mask", "mask", None),
    "8": ("bottom_mask", "mask", None),
    "10": ("dimension", "mechanical", None),
    "11": ("drill", "drill", None),
    "12": ("keepout", "restrict", None),
    "13": ("documentation", "documentation", None),
}

STD_LAYER_BY_NAME: dict[str, tuple[str, str, int | None]] = {
    "toplayer": ("top_copper", "electrical", 1),
    "bottomlayer": ("bottom_copper", "electrical", 2),
    "topsilklayer": ("top_silkscreen", "silkscreen", None),
    "bottomsilklayer": ("bottom_silkscreen", "silkscreen", None),
    "boardoutline": ("dimension", "mechanical", None),
    "outline": ("dimension", "mechanical", None),
}

PRO_LAYER_BY_NAME: dict[str, tuple[str, str, int | None]] = {
    "toplayer": ("top_copper", "electrical", 1),
    "bottomlayer": ("bottom_copper", "electrical", 2),
    "topsilklayer": ("top_silkscreen", "silkscreen", None),
    "bottomsilklayer": ("bottom_silkscreen", "silkscreen", None),
    "topsilkscreenlayer": ("top_silkscreen", "silkscreen", None),
    "bottomsilkscreenlayer": ("bottom_silkscreen", "silkscreen", None),
    "topsoldermasklayer": ("top_mask", "mask", None),
    "bottomsoldermasklayer": ("bottom_mask", "mask", None),
    "topsolderpastelayer": ("top_paste", "paste", None),
    "bottomsolderpastelayer": ("bottom_paste", "paste", None),
    "toppastemasklayer": ("top_paste", "paste", None),
    "bottompastemasklayer": ("bottom_paste", "paste", None),
    "boardoutlinelayer": ("dimension", "mechanical", None),
    "documentlayer": ("documentation", "documentation", None),
    "multilayer": ("multi", "electrical", None),
    "keepoutlayer": ("keepout", "restrict", None),
    "drilldrawinglayer": ("drill", "drill", None),
    "documentationlayer": ("documentation", "documentation", None),
    "mechanicallayer": ("mechanical", "mechanical", None),
    "componentshapelayer": ("documentation", "documentation", None),
    "componentmarkinglayer": ("documentation", "documentation", None),
    "pinsolderinglayer": ("documentation", "documentation", None),
    "holelayer": ("drill", "drill", None),
    "ratlinelayer": ("documentation", "documentation", None),
    "topassemblylayer": ("documentation", "documentation", None),
    "bottomassemblylayer": ("documentation", "documentation", None),
}

PRO_LAYER_BY_ID: dict[str, tuple[str, str, int | None]] = {
    "1": ("top_copper", "electrical", 1),
    "2": ("bottom_copper", "electrical", 2),
    "3": ("top_silkscreen", "silkscreen", None),
    "4": ("bottom_silkscreen", "silkscreen", None),
    "5": ("top_mask", "mask", None),
    "6": ("bottom_mask", "mask", None),
    "7": ("top_paste", "paste", None),
    "8": ("bottom_paste", "paste", None),
    "11": ("dimension", "mechanical", None),
    "12": ("multi", "electrical", None),
    "13": ("documentation", "documentation", None),
    "14": ("mechanical", "mechanical", None),
    "47": ("drill", "drill", None),
    "56": ("drill", "drill", None),
}


@dataclass
class LayerMappingReport:
    source_format: SourceFormat
    entries: list[dict[str, Any]] = field(default_factory=list)

    def as_text(self) -> str:
        lines = [f"Layer Mapping Report ({self.source_format.value})", ""]
        if not self.entries:
            lines.append("No source layers were provided by parser.")
            return "\n".join(lines)

        for entry in self.entries:
            status = "LOSSY" if entry.get("lossy") else "OK"
            lines.append(
                f"[{status}] source={entry['source_id']}:{entry['source_name']} -> "
                f"target={entry['mapped_name']} ({entry['category']})"
            )
        return "\n".join(lines)


def map_layers(
    source_format: SourceFormat,
    raw_layers: list[dict[str, Any]],
) -> tuple[list[Layer], LayerMappingReport, list[Any]]:
    mapped_layers: list[Layer] = []
    report = LayerMappingReport(source_format=source_format)
    events = []
    seen_keys: set[tuple[str, str]] = set()

    for layer_raw in raw_layers:
        source_id = str(layer_raw.get("id", layer_raw.get("layerId", ""))).strip() or "unknown"
        source_name = str(layer_raw.get("name", layer_raw.get("layerName", source_id))).strip() or source_id
        dedupe_key = (source_id, source_name)
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        normalized_name = source_name.lower().replace(" ", "")

        mapped_name: str
        category: str
        copper_index: int | None
        lossy = False

        if source_format == SourceFormat.EASYEDA_STD:
            mapped = STD_LAYER_BY_ID.get(source_id) or STD_LAYER_BY_NAME.get(normalized_name)
        else:
            mapped = PRO_LAYER_BY_ID.get(source_id) or PRO_LAYER_BY_NAME.get(normalized_name)
            if mapped is None and normalized_name.startswith("innerlayer"):
                suffix = normalized_name.replace("innerlayer", "")
                try:
                    idx = int(suffix)
                    mapped = (f"inner{idx}_copper", "electrical", idx)
                except ValueError:
                    mapped = None
            if mapped is None and source_id.isdigit():
                source_idx = int(source_id)
                if 15 <= source_idx <= 46:
                    mapped = (f"inner{source_idx - 14}_copper", "electrical", source_idx - 14)

        if mapped is None and "inner" in normalized_name and "layer" in normalized_name:
            digits = "".join(ch for ch in normalized_name if ch.isdigit())
            idx = int(digits) if digits else 3
            mapped = (f"inner{idx}_copper", "electrical", idx)

        if mapped is None:
            mapped_name = "documentation"
            category = "documentation"
            copper_index = None
            lossy = True
            events.append(
                project_event(
                    Severity.WARNING,
                    "LAYER_MAP_LOSSY",
                    f"Layer '{source_name}' mapped to documentation due to missing rule",
                    {"source_id": source_id, "source_name": source_name},
                )
            )
        else:
            mapped_name, category, copper_index = mapped

        mapped_layer = Layer(
            source_id=source_id,
            source_name=source_name,
            family=source_format.value,
            mapped_name=mapped_name,
            category=category,
            copper_index=copper_index,
            lossy=lossy,
        )
        mapped_layers.append(mapped_layer)
        report.entries.append(
            {
                "source_id": source_id,
                "source_name": source_name,
                "mapped_name": mapped_name,
                "category": category,
                "copper_index": copper_index,
                "lossy": lossy,
            }
        )

    return mapped_layers, report, events
