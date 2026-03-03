from __future__ import annotations

from pathlib import Path

from easyeda2fusion.builders.board_reconstruction import BoardReconstructionBuilder
from easyeda2fusion.builders.schematic_reconstruction import SchematicReconstructionBuilder
from easyeda2fusion.model import Project


def emit_rebuild_scripts(
    project: Project,
    out_dir: Path,
    generated_library_path: Path | None = None,
    external_library_paths: list[Path] | None = None,
) -> dict[str, Path]:
    scripts: dict[str, Path] = {}

    schematic_builder = SchematicReconstructionBuilder()
    board_builder = BoardReconstructionBuilder()

    preamble = _script_preamble(project, generated_library_path, external_library_paths or [])
    library_refs = _library_reference_map(generated_library_path, external_library_paths or [])
    sch_lines = schematic_builder.build_commands(project, library_paths=library_refs)
    sch_path = out_dir / "rebuild_schematic.scr"
    _write_lines(sch_path, [*preamble, *sch_lines])
    scripts["schematic"] = sch_path

    brd_lines = board_builder.build_commands(project)
    brd_path = out_dir / "rebuild_board.scr"
    _write_lines(brd_path, [*preamble, *brd_lines])
    scripts["board"] = brd_path

    return scripts


def _script_preamble(
    project: Project,
    generated_library_path: Path | None,
    external_library_paths: list[Path],
) -> list[str]:
    lines: list[str] = []
    lines.append("SET CONFIRM OFF;")
    lines.append("SET Warning.PartHasNoUserDefinableValue 0;")
    lines.append("SET Warning.PartNoUserDefinableValueAssigned 0;")
    if generated_library_path is not None:
        path = _to_script_path(generated_library_path)
        lines.append(f"USE '{path}';")
    for library_path in external_library_paths:
        path = _to_script_path(library_path)
        lines.append(f"USE '{path}';")

    if lines:
        lines.append("")
    return lines


def _library_reference_map(
    generated_library_path: Path | None,
    external_library_paths: list[Path],
) -> dict[str, str]:
    refs: dict[str, str] = {}

    if generated_library_path is not None:
        refs["easyeda_generated"] = _to_script_path(generated_library_path)

    for library_path in external_library_paths:
        stem = library_path.stem.strip()
        if not stem:
            continue
        refs.setdefault(stem, _to_script_path(library_path))

    return refs


def _to_script_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/")


def _write_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(line)
            if not line.endswith("\n"):
                f.write("\n")
