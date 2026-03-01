from __future__ import annotations

from pathlib import Path
from typing import Any

from easyeda2fusion.utils.io import dump_json


def write_summary(summary: dict[str, Any], out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "summary.json"
    text_path = out_dir / "summary.txt"

    dump_json(json_path, summary)

    lines = [
        "Conversion Summary",
        "",
    ]
    for key, value in summary.items():
        lines.append(f"{key}: {value}")

    text_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"json": json_path, "text": text_path}
