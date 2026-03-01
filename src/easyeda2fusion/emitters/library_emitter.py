from __future__ import annotations

from pathlib import Path

from easyeda2fusion.matchers.library_matcher import MatchContext
from easyeda2fusion.model import Project
from easyeda2fusion.utils.io import dump_json


def emit_library_artifacts(project: Project, match_ctx: MatchContext, out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = out_dir / "library_manifest.json"
    dump_json(
        manifest_path,
        {
            "matched": [
                {
                    "refdes": match.refdes,
                    "stage": match.stage,
                    "matched": match.matched,
                    "target_device": match.target_device,
                    "target_package": match.target_package,
                    "reason": match.reason,
                    "candidates": match.candidates,
                    "created_new_part": match.created_new_part,
                }
                for match in project.library_matches
            ],
            "generated_parts": [
                {
                    "symbol": part.symbol.symbol_id,
                    "package": part.package.package_id,
                    "device": part.device.device_id,
                    "source": part.source,
                }
                for part in match_ctx.new_library_parts
            ],
        },
    )

    text_path = out_dir / "library_manifest.txt"
    lines = ["Library Match Report", ""]
    for match in project.library_matches:
        state = "MATCHED" if match.matched else "UNRESOLVED"
        lines.append(
            f"[{state}] {match.refdes} stage={match.stage} device={match.target_device or '-'} package={match.target_package or '-'}"
        )
        if match.reason:
            lines.append(f"  reason={match.reason}")
        if match.candidates:
            lines.append(f"  candidates={', '.join(match.candidates)}")
    text_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return {"json": manifest_path, "text": text_path}
