from __future__ import annotations

from pathlib import Path
from typing import Any

from easyeda2fusion.model import Project
from easyeda2fusion.utils.io import dump_json


def emit_normalized_manifest(project: Project, out_dir: Path) -> Path:
    path = out_dir / "normalized_project.json"
    dump_json(path, project.to_dict())
    return path


def emit_machine_manifest(payload: dict[str, Any], out_dir: Path, name: str) -> Path:
    path = out_dir / name
    dump_json(path, payload)
    return path
