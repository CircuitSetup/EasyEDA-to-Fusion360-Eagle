from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from easyeda2fusion.builders.normalizer import Normalizer
from easyeda2fusion.builders.schematic_inference import (
    SchematicInferenceReport,
    infer_schematic_from_board,
)
from easyeda2fusion.emitters.eagle_project_emitter import emit_project_artifacts
from easyeda2fusion.emitters.generated_library_emitter import emit_generated_library
from easyeda2fusion.emitters.json_emitter import emit_machine_manifest, emit_normalized_manifest
from easyeda2fusion.emitters.library_emitter import emit_library_artifacts
from easyeda2fusion.emitters.script_emitter import emit_rebuild_scripts
from easyeda2fusion.matchers.library_loader import load_library_entries
from easyeda2fusion.matchers.library_matcher import LibraryEntry, LibraryMatcher
from easyeda2fusion.model import ConversionMode, MatchMode, Severity, SourceFormat, project_event
from easyeda2fusion.parsers import parse_easyeda_files
from easyeda2fusion.reports.layer_mapping import write_layer_mapping_report
from easyeda2fusion.reports.schematic_pipeline import write_schematic_pipeline_reports
from easyeda2fusion.reports.summary import write_summary
from easyeda2fusion.reports.unresolved_parts import write_unresolved_reports
from easyeda2fusion.reports.validation import validate_project, write_validation_report
from easyeda2fusion.utils.io import ensure_dir
from easyeda2fusion.utils.logging import configure_logging

log = logging.getLogger(__name__)

AmbiguityResolver = Callable[[object, List[LibraryEntry]], Optional[LibraryEntry]]
ProgressCallback = Callable[[int, str], None]


@dataclass
class ConversionConfig:
    input_files: list[Path]
    output_dir: Path
    mode: ConversionMode = ConversionMode.FULL
    match_mode: MatchMode = MatchMode.AUTO
    source_format: SourceFormat | None = None
    library_path: Path | None = None
    resistor_library_path: Path | None = None
    capacitor_library_path: Path | None = None
    use_default_fusion_libraries: bool = True
    schematic_layout_mode: str = "board"
    verbose: bool = False


@dataclass
class ConversionResult:
    output_dir: Path
    summary: dict[str, object]
    generated_files: dict[str, Path] = field(default_factory=dict)


class Converter:
    def run(
        self,
        config: ConversionConfig,
        resolver: AmbiguityResolver | None = None,
        progress: ProgressCallback | None = None,
    ) -> ConversionResult:
        self._emit_progress(progress, 0, "Starting conversion")
        input_files = self._resolve_input_files(config.input_files)
        self._emit_progress(progress, 5, "Resolved input paths")

        ensure_dir(config.output_dir)
        log_path = config.output_dir / "conversion.log"
        configure_logging(log_path=log_path, verbose=config.verbose)

        self._emit_progress(progress, 10, "Parsing EasyEDA input")
        parsed = parse_easyeda_files(input_files, forced_format=config.source_format)
        self._emit_progress(progress, 20, "Normalizing source model")
        normalization = Normalizer().normalize(parsed)
        project = normalization.project
        project.metadata["schematic_layout_mode"] = self._normalize_schematic_layout_mode(
            config.schematic_layout_mode
        )
        # Keep schematic imports on Fusion/EAGLE default grid behavior.
        # The schematic builder uses this flag to avoid forcing GRID MM 0.1.
        project.metadata["schematic_snap_to_default_grid"] = True

        self._apply_mode(project, config.mode)
        self._emit_progress(progress, 30, "Applying conversion mode")

        inference_report: SchematicInferenceReport | None = None
        if config.mode == ConversionMode.BOARD_INFER_SCHEMATIC:
            self._emit_progress(progress, 35, "Inferring schematic from board")
            inference_report = infer_schematic_from_board(project, force=True)
        elif config.mode == ConversionMode.FULL and project.board is not None and not project.sheets:
            self._emit_progress(progress, 35, "Inferring schematic from board")
            inference_report = infer_schematic_from_board(project, force=True)
            project.events.append(
                project_event(
                    Severity.WARNING,
                    "NO_SOURCE_SCHEMATIC_INFERRED",
                    "No schematic source present; inferred from board for continuity",
                )
            )
        elif config.mode == ConversionMode.FULL and project.board is not None and self._is_schematic_weak(project):
            self._emit_progress(progress, 35, "Augmenting weak schematic from board")
            inference_report = infer_schematic_from_board(project, force=True)
            project.events.append(
                project_event(
                    Severity.WARNING,
                    "WEAK_SOURCE_SCHEMATIC_INFERRED",
                    "Source schematic lacks sufficient connectivity/components; augmented with board-inferred schematic",
                )
            )

        self._emit_progress(progress, 45, "Loading library entries")
        library_entries = load_library_entries(
            config.library_path,
            resistor_library_path=config.resistor_library_path,
            capacitor_library_path=config.capacitor_library_path,
            use_default_fusion_libraries=config.use_default_fusion_libraries,
        )
        passive_preferred_paths = self._build_passive_library_preferences(config)
        matcher = LibraryMatcher()
        self._emit_progress(progress, 60, "Matching parts to libraries")
        match_ctx = matcher.match(
            project=project,
            library_entries=library_entries,
            match_mode=config.match_mode,
            resolver=resolver,
            preferred_library_paths_by_class=passive_preferred_paths,
        )

        self._cross_check_schematic_vs_board(project)
        self._emit_progress(progress, 68, "Cross-checking schematic vs board")

        artifacts_dir = ensure_dir(config.output_dir / "artifacts")
        scripts_dir = ensure_dir(config.output_dir / "scripts")
        reports_dir = ensure_dir(config.output_dir / "reports")
        manifests_dir = ensure_dir(config.output_dir / "manifests")
        library_dir = ensure_dir(config.output_dir / "library")
        self._emit_progress(progress, 72, "Preparing output directories")

        generated_files: dict[str, Path] = {}

        self._emit_progress(progress, 76, "Writing manifests")
        generated_files["normalized_manifest"] = emit_normalized_manifest(project, manifests_dir)
        generated_files["source_manifest"] = emit_machine_manifest(
            {
                "source_format": parsed.source_format.value,
                "input_files": parsed.input_files,
                "metadata": parsed.metadata,
                "events": [
                    {
                        "severity": event.severity.value,
                        "code": event.code,
                        "message": event.message,
                        "context": event.context,
                    }
                    for event in parsed.events
                ],
            },
            manifests_dir,
            "source_manifest.json",
        )

        if inference_report is not None:
            generated_files["inference_manifest"] = emit_machine_manifest(
                {
                    "inferred": inference_report.inferred,
                    "inferred_nets": inference_report.inferred_nets,
                    "ambiguous_pin_mappings": inference_report.ambiguous_pin_mappings,
                    "uncertain_components": inference_report.uncertain_components,
                    "manual_review_items": inference_report.manual_review_items,
                },
                manifests_dir,
                "inferred_schematic_manifest.json",
            )

        self._emit_progress(progress, 82, "Emitting project artifacts")
        generated_files.update(
            {f"artifact_{k}": v for k, v in emit_project_artifacts(project, artifacts_dir).items()}
        )

        self._emit_progress(progress, 86, "Emitting libraries and rebuild scripts")
        generated_library_path = emit_generated_library(match_ctx, library_dir, project=project)
        if generated_library_path is not None:
            generated_files["library_generated_lbr"] = generated_library_path

        generated_files.update(
            {
                f"script_{k}": v
                for k, v in emit_rebuild_scripts(
                    project,
                    scripts_dir,
                    generated_library_path=generated_library_path,
                    external_library_paths=[Path(item) for item in sorted(match_ctx.used_external_library_paths)],
                ).items()
            }
        )
        generated_files.update(
            {f"library_{k}": v for k, v in emit_library_artifacts(project, match_ctx, library_dir).items()}
        )

        self._emit_progress(progress, 92, "Generating reports and validation")
        generated_files["layer_mapping_report"] = write_layer_mapping_report(
            normalization.layer_report,
            reports_dir,
        )

        unresolved_paths = write_unresolved_reports(match_ctx.unresolved_parts, reports_dir)
        generated_files.update({f"unresolved_{k}": v for k, v in unresolved_paths.items()})

        validation = validate_project(project, match_ctx, inference_report)
        validation_paths = write_validation_report(validation, reports_dir)
        generated_files.update({f"validation_{k}": v for k, v in validation_paths.items()})
        schematic_pipeline_paths = write_schematic_pipeline_reports(project, reports_dir)
        generated_files.update(
            {f"schematic_pipeline_{k}": v for k, v in schematic_pipeline_paths.items()}
        )

        summary_payload = self._build_summary(
            project=project,
            mode=config.mode,
            source_format=parsed.source_format,
            match_ctx=match_ctx,
            inference_report=inference_report,
            validation=validation,
        )
        summary_paths = write_summary(summary_payload, config.output_dir)
        generated_files.update({f"summary_{k}": v for k, v in summary_paths.items()})
        generated_files["conversion_log"] = log_path
        self._emit_progress(progress, 100, "Conversion complete")

        return ConversionResult(output_dir=config.output_dir, summary=summary_payload, generated_files=generated_files)

    @staticmethod
    def _emit_progress(callback: ProgressCallback | None, percent: int, message: str) -> None:
        if callback is None:
            return
        pct = max(0, min(int(percent), 100))
        callback(pct, str(message))

    @staticmethod
    def _resolve_input_files(input_paths: list[Path]) -> list[Path]:
        if not input_paths:
            raise ValueError("No input files provided.")

        resolved: list[Path] = []

        for raw_path in input_paths:
            candidate = Path(raw_path).expanduser().resolve()

            if not candidate.exists():
                raise FileNotFoundError(f"Input path does not exist: {candidate}")

            if candidate.is_file():
                resolved.append(candidate)
                continue

            if candidate.is_dir():
                json_files = sorted(candidate.glob("*.json"))
                if not json_files:
                    raise ValueError(
                        f"Input path is a directory with no .json files: {candidate}"
                    )
                resolved.extend(json_files)
                continue

            raise ValueError(f"Unsupported input path type: {candidate}")

        unique: list[Path] = []
        seen: set[str] = set()
        for path in resolved:
            key = str(path)
            if key not in seen:
                seen.add(key)
                unique.append(path)

        if not unique:
            raise ValueError("No valid input files were resolved from --input.")

        return unique

    @staticmethod
    def _apply_mode(project, mode: ConversionMode) -> None:
        if mode == ConversionMode.SCHEMATIC_ONLY:
            project.board = None
        elif mode == ConversionMode.BOARD_ONLY:
            project.sheets = []
            # Keep source nets if they exist, but remove schematic-only node intent.
            for net in project.nets:
                net.nodes = []
        elif mode == ConversionMode.BOARD_INFER_SCHEMATIC:
            project.sheets = []
            for net in project.nets:
                net.nodes = []

    @staticmethod
    def _cross_check_schematic_vs_board(project) -> None:
        if project.board is None or not project.sheets:
            return

        sch_net_names = {net.name for net in project.nets if net.nodes}
        brd_net_names = {
            *(track.net for track in project.board.tracks if track.net),
            *(via.net for via in project.board.vias if via.net),
            *(region.net for region in project.board.regions if region.net),
        }

        only_sch = sorted(sch_net_names - brd_net_names)
        only_brd = sorted(brd_net_names - sch_net_names)

        if only_sch:
            project.events.append(
                project_event(
                    Severity.WARNING,
                    "NET_MISMATCH_SCHEMATIC_ONLY",
                    "Nets present in schematic but not detected on board",
                    {"nets": only_sch[:100]},
                )
            )

        if only_brd:
            project.events.append(
                project_event(
                    Severity.WARNING,
                    "NET_MISMATCH_BOARD_ONLY",
                    "Nets present in board but not detected in schematic",
                    {"nets": only_brd[:100]},
                )
            )

    @staticmethod
    def _build_summary(
        project,
        mode: ConversionMode,
        source_format: SourceFormat,
        match_ctx,
        inference_report: SchematicInferenceReport | None,
        validation,
    ) -> dict[str, object]:
        warnings = sum(1 for event in project.events if event.severity in {Severity.WARNING, Severity.ERROR})

        schematic_state = "skipped"
        if mode == ConversionMode.BOARD_ONLY:
            schematic_state = "skipped"
        elif inference_report is not None and inference_report.inferred:
            schematic_state = "inferred"
        elif project.sheets:
            schematic_state = "converted"

        board_state = "converted" if project.board is not None and mode != ConversionMode.SCHEMATIC_ONLY else "skipped"

        return {
            "detected_source_type": source_format.value,
            "conversion_mode": mode.value,
            "schematic_status": schematic_state,
            "board_status": board_state,
            "parts_auto_matched": match_ctx.summary.auto_matched,
            "parts_requiring_user_input": match_ctx.summary.ambiguous,
            "new_library_parts_created": match_ctx.summary.created_new_parts,
            "unresolved_parts": match_ctx.summary.unresolved,
            "major_warnings": warnings,
            "converted_with_warnings": validation.converted_with_warnings,
        }

    @staticmethod
    def _is_schematic_weak(project) -> bool:
        if project.board is None:
            return False
        if not project.sheets:
            return True

        board_component_count = len(project.components)
        sheet_component_count = sum(len(sheet.components) for sheet in project.sheets)
        logical_net_count = sum(1 for net in project.nets if net.nodes)

        if logical_net_count > 0 and sheet_component_count >= max(1, board_component_count // 3):
            return False

        meaningful_sheet_components = 0
        for sheet in project.sheets:
            for refdes in sheet.components:
                ref = str(refdes or "")
                if not ref:
                    continue
                if ref.startswith("e") and ref[1:].isdigit():
                    continue
                meaningful_sheet_components += 1

        return logical_net_count == 0 or meaningful_sheet_components < max(2, board_component_count // 4)

    @staticmethod
    def _build_passive_library_preferences(config: ConversionConfig) -> dict[str, set[str]]:
        preferred: dict[str, set[str]] = {}
        if config.resistor_library_path is not None:
            paths = _expand_library_preference_paths(config.resistor_library_path)
            if paths:
                preferred["resistor"] = paths
        if config.capacitor_library_path is not None:
            paths = _expand_library_preference_paths(config.capacitor_library_path)
            if paths:
                preferred["capacitor"] = paths
        return preferred

    @staticmethod
    def _normalize_schematic_layout_mode(value: str | None) -> str:
        token = str(value or "").strip().lower()
        if token in {"board", "clustered", "hybrid", "human"}:
            return token
        return "board"


def _expand_library_preference_paths(path: Path) -> set[str]:
    candidate = Path(path).expanduser().resolve()
    if not candidate.exists():
        return set()
    if candidate.is_file():
        return {str(candidate)}

    out: set[str] = set()
    for lib in sorted(candidate.rglob("*.lbr")):
        out.add(str(lib.resolve()))
    return out
