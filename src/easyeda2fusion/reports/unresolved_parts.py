from __future__ import annotations

import csv
from pathlib import Path

from easyeda2fusion.matchers.library_matcher import UnresolvedPart
from easyeda2fusion.utils.io import dump_json


def write_unresolved_reports(parts: list[UnresolvedPart], out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "unresolved_parts.csv"
    json_path = out_dir / "unresolved_parts.json"

    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "refdes",
            "source_name",
            "package",
            "value",
            "reason",
            "required_action",
            "attributes",
        ])
        for part in parts:
            writer.writerow(
                [
                    part.refdes,
                    part.source_name,
                    part.package or "",
                    part.value,
                    part.reason,
                    part.required_action,
                    ";".join(f"{k}={v}" for k, v in sorted(part.attributes.items())),
                ]
            )

    dump_json(
        json_path,
        {
            "count": len(parts),
            "parts": [
                {
                    "refdes": part.refdes,
                    "source_name": part.source_name,
                    "package": part.package,
                    "value": part.value,
                    "attributes": part.attributes,
                    "reason": part.reason,
                    "required_action": part.required_action,
                }
                for part in parts
            ],
        },
    )

    return {"csv": csv_path, "json": json_path}
