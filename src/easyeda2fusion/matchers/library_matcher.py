from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import html
import math
from pathlib import Path
import re
from typing import Callable, List, Optional
import xml.etree.ElementTree as ET

from easyeda2fusion.builders.library_builder import GeneratedLibraryPart, LibraryBuilder
from easyeda2fusion.builders.net_aliases import project_track_net_aliases
from easyeda2fusion.builders.package_utils import resolve_component_package as _shared_resolve_component_package
from easyeda2fusion.model import Component, LibraryMatch, MatchMode, Package, Project, Severity, Side, project_event


@dataclass
class LibraryEntry:
    device_name: str
    package_name: str
    symbol_name: str
    mpn: str | None = None
    aliases: list[str] = field(default_factory=list)
    component_class: str | None = None
    library_name: str | None = None
    add_token: str | None = None
    library_path: str | None = None


@dataclass
class UnresolvedPart:
    refdes: str
    source_name: str
    package: str | None
    value: str
    attributes: dict[str, str]
    reason: str
    required_action: str


@dataclass
class MatchSummary:
    auto_matched: int = 0
    prompted: int = 0
    created_new_parts: int = 0
    unresolved: int = 0
    ambiguous: int = 0


@dataclass
class MatchContext:
    unresolved_parts: list[UnresolvedPart] = field(default_factory=list)
    new_library_parts: list[GeneratedLibraryPart] = field(default_factory=list)
    summary: MatchSummary = field(default_factory=MatchSummary)
    used_external_library_paths: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class _ExternalPadGeometry:
    x_mm: float
    y_mm: float
    width_mm: float
    height_mm: float
    drill_mm: float | None = None


@dataclass(frozen=True)
class _ExternalGeometryTransform:
    offset_x_mm: float
    offset_y_mm: float
    rotation_deg: int = 0


@dataclass(frozen=True)
class _LibraryEntryFeature:
    entry: LibraryEntry
    norm_device_name: str
    norm_package_name: str
    norm_component_class: str
    norm_mpn: str
    norm_aliases: tuple[str, ...]
    package_variants: frozenset[str]
    part_number_keys: frozenset[str]
    mpn_part_number_keys: frozenset[str]


@dataclass
class _LibraryEntryIndex:
    features: list[_LibraryEntryFeature]
    by_norm_mpn: dict[str, list[_LibraryEntryFeature]]
    by_norm_device_name: dict[str, list[_LibraryEntryFeature]]
    by_norm_alias: dict[str, list[_LibraryEntryFeature]]
    by_part_number_key: dict[str, list[_LibraryEntryFeature]]
    by_norm_component_class: dict[str, list[_LibraryEntryFeature]]


AmbiguityResolver = Callable[[Component, List[LibraryEntry]], Optional[LibraryEntry]]


class LibraryMatcher:
    def __init__(self) -> None:
        self._builder = LibraryBuilder()

    def match(
        self,
        project: Project,
        library_entries: list[LibraryEntry],
        match_mode: MatchMode,
        resolver: AmbiguityResolver | None = None,
        preferred_library_paths_by_class: dict[str, set[str]] | None = None,
    ) -> MatchContext:
        ctx = MatchContext()
        self._builder.configure(project.metadata if isinstance(project.metadata, dict) else {})
        preferred_paths_by_class = _normalize_library_preference_paths(preferred_library_paths_by_class or {})
        entry_index = _build_library_entry_index(library_entries)
        package_lookup: dict[str, Package] = {}
        for pkg in project.packages:
            package_lookup[pkg.package_id] = pkg
            package_lookup[pkg.name] = pkg
        resolution_cache: dict[tuple[str, str, str], LibraryEntry | None] = {}
        pin_net_hints_by_ref = _component_pin_net_hints(project)
        external_package_cache: dict[tuple[str, str], dict[str, _ExternalPadGeometry] | None] = {}

        for component in project.components:
            if component.package_id:
                package_obj = package_lookup.get(component.package_id)
                if package_obj is not None and package_obj.name:
                    component.attributes.setdefault("package_name", package_obj.name)

            signature = self._signature(component)
            selected: LibraryEntry | None = None
            selected_stage: str | None = None
            staged_candidates: list[LibraryEntry] = []
            component_class = _component_class(component)
            is_passive = component_class in {"resistor", "capacitor"}

            stage_order: list[tuple[str, list[LibraryEntry]]] = []
            if match_mode == MatchMode.PACKAGE_FIRST and is_passive:
                stage_order.append(("stage2_package_class", self._stage2_package_and_class(component, entry_index)))
                stage_order.append(("stage1_exact", self._stage1_exact(component, entry_index)))
                stage_order.append(("stage1_part_number", self._stage1_part_number(component, entry_index)))
            else:
                stage_order.append(("stage1_exact", self._stage1_exact(component, entry_index)))
                stage_order.append(("stage1_part_number", self._stage1_part_number(component, entry_index)))
                if match_mode != MatchMode.STRICT:
                    stage_order.append(
                        ("stage2_package_class", self._stage2_package_and_class(component, entry_index))
                    )

            for stage_name, candidates in stage_order:
                if not candidates:
                    continue
                candidates = _dedupe_candidate_entries(candidates)
                staged_candidates = candidates
                if len(candidates) == 1:
                    selected = candidates[0]
                    selected_stage = stage_name
                else:
                    stage_selected = _prefer_stage_candidate(component, candidates, stage_name)
                    passive_selected = _prefer_passive_external_candidate(
                        component,
                        candidates,
                        preferred_paths=preferred_paths_by_class.get(component_class, set()),
                    )
                    if stage_selected is not None:
                        selected = stage_selected
                        selected_stage = f"{stage_name}_preferred"
                    if passive_selected is not None:
                        selected = passive_selected
                        selected_stage = f"{stage_name}_passive_external"
                    if selected is None:
                        selected = self._resolve_ambiguity(
                            component,
                            candidates,
                            match_mode,
                            resolver,
                            resolution_cache,
                            signature,
                            ctx,
                            stage=stage_name,
                        )
                        if selected is not None:
                            selected_stage = stage_name
                break

            if selected is not None:
                component.package_id = component.package_id or selected.package_name

                if selected.add_token:
                    source_package = _resolve_component_package(component, package_lookup)
                    forced_local_reason = _force_local_passive_reason(component, source_package)
                    selected_for_external = selected
                    compatible_external = False
                    compatibility_reason = forced_local_reason
                    external_candidates: list[LibraryEntry] = [selected]
                    external_candidates.extend(
                        candidate
                        for candidate in staged_candidates
                        if candidate is not selected and candidate.add_token
                    )

                    if not forced_local_reason:
                        for candidate in external_candidates:
                            is_compatible, reason = _external_package_match_is_compatible(
                                component=component,
                                selected=candidate,
                                source_package=source_package,
                                cache=external_package_cache,
                                strict_board_fidelity=project.board is not None,
                            )
                            if is_compatible and _is_safe_external_add_token(str(candidate.add_token or "")):
                                selected_for_external = candidate
                                compatible_external = True
                                compatibility_reason = reason
                                break
                            if candidate is selected and compatibility_reason is None:
                                compatibility_reason = reason
                    selected = selected_for_external
                    if not compatible_external:
                        project.events.append(
                            project_event(
                                Severity.WARNING,
                                "EXTERNAL_PACKAGE_GEOMETRY_MISMATCH",
                                f"External package geometry mismatch for {component.refdes}; generating exact local part",
                                {
                                    "refdes": component.refdes,
                                    "library": selected.library_name,
                                    "device": selected.device_name,
                                    "package": selected.package_name,
                                    "reason": compatibility_reason or "geometry_mismatch",
                                },
                            )
                        )
                    elif _is_safe_external_add_token(selected.add_token):
                        if compatibility_reason and compatibility_reason.startswith("relaxed_"):
                            project.events.append(
                                project_event(
                                    Severity.INFO,
                                    "EXTERNAL_PACKAGE_GEOMETRY_RELAXED",
                                    f"Accepted relaxed external package geometry for {component.refdes}",
                                    {
                                        "refdes": component.refdes,
                                        "library": selected.library_name,
                                        "device": selected.device_name,
                                        "package": selected.package_name,
                                        "reason": compatibility_reason,
                                    },
                                )
                            )
                        component.device_id = selected.add_token
                        if selected.library_path:
                            ctx.used_external_library_paths.add(selected.library_path)
                        ctx.summary.auto_matched += 1
                        project.library_matches.append(
                            LibraryMatch(
                                refdes=component.refdes,
                                stage=selected_stage or "matched",
                                matched=True,
                                target_device=component.device_id,
                                target_package=component.package_id,
                            )
                        )
                        continue

                    if compatible_external and not _is_safe_external_add_token(selected.add_token):
                        project.events.append(
                            project_event(
                                Severity.WARNING,
                                "EXTERNAL_ADD_TOKEN_UNSAFE",
                                f"Matched external token is not safe for scripted ADD; generating local part for {component.refdes}",
                                {
                                    "refdes": component.refdes,
                                    "add_token": selected.add_token,
                                },
                            )
                        )

                generated_from_selected, reason_from_selected = self._builder.synthesize_missing_part(
                    component,
                    package_lookup,
                    pin_net_hints=pin_net_hints_by_ref.get(component.refdes, {}),
                )
                if generated_from_selected is not None:
                    ctx.new_library_parts.append(generated_from_selected)
                    ctx.summary.created_new_parts += 1
                    component.symbol_id = generated_from_selected.symbol.symbol_id
                    component.package_id = generated_from_selected.package.package_id
                    component.device_id = f"easyeda_generated:{generated_from_selected.device.device_id}"
                    project.symbols.append(generated_from_selected.symbol)
                    if generated_from_selected.package.package_id not in package_lookup:
                        project.packages.append(generated_from_selected.package)
                        package_lookup[generated_from_selected.package.package_id] = generated_from_selected.package
                        package_lookup[generated_from_selected.package.name] = generated_from_selected.package
                    project.devices.append(generated_from_selected.device)
                    project.library_matches.append(
                        LibraryMatch(
                            refdes=component.refdes,
                            stage="stage4_create_library_entry_from_generic_match",
                            matched=True,
                            target_device=component.device_id,
                            target_package=generated_from_selected.package.package_id,
                            created_new_part=True,
                        )
                    )
                    continue

                unresolved = UnresolvedPart(
                    refdes=component.refdes,
                    source_name=component.source_name,
                    package=component.package_id,
                    value=component.value,
                    attributes={k: str(v) for k, v in component.attributes.items()},
                    reason=reason_from_selected or "generic_match_without_local_library",
                    required_action="manual_library_import_or_pin_mapping",
                )
                ctx.unresolved_parts.append(unresolved)
                ctx.summary.unresolved += 1
                project.library_matches.append(
                    LibraryMatch(
                        refdes=component.refdes,
                        stage="stage5_unresolved",
                        matched=False,
                        reason=unresolved.reason,
                    )
                )
                continue

            if staged_candidates:
                if match_mode in {MatchMode.AUTO, MatchMode.PACKAGE_FIRST}:
                    generated, reason = self._builder.synthesize_missing_part(
                        component,
                        package_lookup,
                        pin_net_hints=pin_net_hints_by_ref.get(component.refdes, {}),
                    )
                    if generated is not None:
                        ctx.new_library_parts.append(generated)
                        ctx.summary.created_new_parts += 1
                        component.symbol_id = generated.symbol.symbol_id
                        component.package_id = generated.package.package_id
                        component.device_id = f"easyeda_generated:{generated.device.device_id}"
                        project.symbols.append(generated.symbol)
                        if generated.package.package_id not in package_lookup:
                            project.packages.append(generated.package)
                            package_lookup[generated.package.package_id] = generated.package
                            package_lookup[generated.package.name] = generated.package
                        project.devices.append(generated.device)
                        project.library_matches.append(
                            LibraryMatch(
                                refdes=component.refdes,
                                stage="stage4_create_library_entry_after_ambiguity",
                                matched=True,
                                target_device=component.device_id,
                                target_package=generated.package.package_id,
                                reason=reason or "ambiguous_candidates_fell_back_to_generated_part",
                                candidates=[entry.device_name for entry in staged_candidates],
                                created_new_part=True,
                            )
                        )
                        continue

                unresolved = UnresolvedPart(
                    refdes=component.refdes,
                    source_name=component.source_name,
                    package=component.package_id,
                    value=component.value,
                    attributes={k: str(v) for k, v in component.attributes.items()},
                    reason="ambiguous_candidates_no_selection",
                    required_action="manual_candidate_selection",
                )
                ctx.unresolved_parts.append(unresolved)
                ctx.summary.unresolved += 1
                project.library_matches.append(
                    LibraryMatch(
                        refdes=component.refdes,
                        stage="stage3_ambiguity_resolution",
                        matched=False,
                        reason=unresolved.reason,
                        candidates=[entry.device_name for entry in staged_candidates],
                    )
                )
                project.events.append(
                    project_event(
                        Severity.WARNING,
                        "AMBIGUOUS_PART_REQUIRES_SELECTION",
                        f"Ambiguous library candidates for {component.refdes}",
                        {
                            "refdes": component.refdes,
                            "candidates": [entry.device_name for entry in staged_candidates],
                        },
                    )
                )
                continue

            generated, reason = self._builder.synthesize_missing_part(
                component,
                package_lookup,
                pin_net_hints=pin_net_hints_by_ref.get(component.refdes, {}),
            )
            if generated is not None:
                ctx.new_library_parts.append(generated)
                ctx.summary.created_new_parts += 1
                component.symbol_id = generated.symbol.symbol_id
                component.package_id = generated.package.package_id
                component.device_id = f"easyeda_generated:{generated.device.device_id}"
                project.symbols.append(generated.symbol)
                if generated.package.package_id not in package_lookup:
                    project.packages.append(generated.package)
                    package_lookup[generated.package.package_id] = generated.package
                    package_lookup[generated.package.name] = generated.package
                project.devices.append(generated.device)
                project.library_matches.append(
                    LibraryMatch(
                        refdes=component.refdes,
                        stage="stage4_create_library_entry",
                        matched=True,
                        target_device=component.device_id,
                        target_package=generated.package.package_id,
                        created_new_part=True,
                    )
                )
                continue

            unresolved = UnresolvedPart(
                refdes=component.refdes,
                source_name=component.source_name,
                package=component.package_id,
                value=component.value,
                attributes={k: str(v) for k, v in component.attributes.items()},
                reason=reason or "no_matching_library_entry",
                required_action="manual_library_import_or_pin_mapping",
            )
            ctx.unresolved_parts.append(unresolved)
            ctx.summary.unresolved += 1
            project.library_matches.append(
                LibraryMatch(
                    refdes=component.refdes,
                    stage="stage5_unresolved",
                    matched=False,
                    reason=unresolved.reason,
                    candidates=[entry.device_name for entry in staged_candidates],
                )
            )
            project.events.append(
                project_event(
                    Severity.WARNING,
                    "UNRESOLVED_PART",
                    f"No reliable target library entry for {component.refdes}",
                    {
                        "refdes": component.refdes,
                        "source_name": component.source_name,
                        "package": component.package_id,
                        "reason": unresolved.reason,
                    },
                )
            )

        _merge_equivalent_generated_parts(project, ctx)
        return ctx

    def _stage1_exact(self, component: Component, entry_index: _LibraryEntryIndex) -> list[LibraryEntry]:
        mpn = _norm(component.mpn)
        src_name = _norm(component.source_name)
        package = _norm(component.package_id)
        class_name = _component_class(component)
        results: list[LibraryEntry] = []
        seen: set[int] = set()

        if mpn and class_name not in {"resistor", "capacitor"}:
            for feature in entry_index.by_norm_mpn.get(mpn, []):
                entry_id = id(feature.entry)
                if entry_id in seen:
                    continue
                seen.add(entry_id)
                results.append(feature.entry)

        if src_name and package:
            for feature in entry_index.by_norm_device_name.get(src_name, []):
                if feature.norm_package_name != package:
                    continue
                entry_id = id(feature.entry)
                if entry_id in seen:
                    continue
                seen.add(entry_id)
                results.append(feature.entry)

            for feature in entry_index.by_norm_alias.get(src_name, []):
                if feature.norm_package_name != package:
                    continue
                entry_id = id(feature.entry)
                if entry_id in seen:
                    continue
                seen.add(entry_id)
                results.append(feature.entry)

        return results

    def _stage1_part_number(self, component: Component, entry_index: _LibraryEntryIndex) -> list[LibraryEntry]:
        class_name = _component_class(component)
        if class_name in {"resistor", "capacitor"}:
            return []
        if class_name == "connector" and _is_screw_terminal_component(component):
            return []

        component_parts = _component_part_number_keys(component)
        if not component_parts:
            return []

        package_keys, package_variants = _non_passive_part_match_package_requirements(component)
        # Non-passive part-number matching requires package compatibility evidence.
        if not package_keys and not package_variants:
            return []

        candidate_features: dict[int, _LibraryEntryFeature] = {}
        for token in component_parts:
            for feature in entry_index.by_part_number_key.get(token, []):
                candidate_features[id(feature.entry)] = feature
        if not candidate_features:
            return []

        scored: list[tuple[int, LibraryEntry]] = []
        for feature in candidate_features.values():
            score = _part_number_match_score_from_feature(
                component_parts,
                feature,
                package_keys,
                package_variants,
            )
            if score > 0:
                scored.append((score, feature.entry))

        if not scored:
            return []

        best_score = max(item[0] for item in scored)
        return [entry for score, entry in scored if score == best_score]

    def _stage2_package_and_class(self, component: Component, entry_index: _LibraryEntryIndex) -> list[LibraryEntry]:
        class_name = _component_class(component)
        if class_name == "connector" and _is_screw_terminal_component(component):
            screw_candidates = _stage2_screw_terminal(
                component,
                [feature.entry for feature in entry_index.features],
            )
            if screw_candidates:
                return screw_candidates

        package_keys = _component_package_keys(component)
        package_variants = _component_package_variants(component)
        class_norm = _norm(class_name)
        results: list[LibraryEntry] = []
        if class_name in {"resistor", "capacitor"}:
            candidate_features = [
                *entry_index.by_norm_component_class.get(class_norm, []),
                *entry_index.by_norm_component_class.get("", []),
            ]
        else:
            candidate_features = entry_index.features

        for feature in candidate_features:
            entry_package = feature.norm_package_name
            entry_variants = feature.package_variants
            entry_class = feature.norm_component_class
            package_ok = bool(entry_package and entry_package in package_keys)
            if not package_ok and package_variants and entry_variants:
                package_ok = bool(package_variants.intersection(entry_variants))
            if not package_ok and package_keys and entry_package:
                package_ok = any(key in entry_package or entry_package in key for key in package_keys)
            class_ok = bool(class_name and entry_class == class_norm)
            if class_name in {"resistor", "capacitor"} and not class_ok:
                continue
            if package_ok and (class_ok or not feature.entry.component_class):
                results.append(feature.entry)
            elif class_ok and package_ok:
                results.append(feature.entry)
        return results

    def _resolve_ambiguity(
        self,
        component: Component,
        candidates: list[LibraryEntry],
        match_mode: MatchMode,
        resolver: AmbiguityResolver | None,
        cache: dict[tuple[str, str, str], LibraryEntry | None],
        signature: tuple[str, str, str],
        ctx: MatchContext,
        stage: str,
    ) -> LibraryEntry | None:
        if signature in cache:
            return cache[signature]

        ctx.summary.ambiguous += 1

        if match_mode == MatchMode.PROMPT and resolver is not None:
            selected = resolver(component, candidates)
            cache[signature] = selected
            if selected is not None:
                ctx.summary.prompted += 1
            return selected

        cache[signature] = None
        return None

    @staticmethod
    def _signature(component: Component) -> tuple[str, str, str]:
        return (_norm(component.source_name), _norm(component.package_id), _norm(component.value))


def _norm(value: str | None) -> str:
    if value is None:
        return ""
    return "".join(ch for ch in str(value).upper() if ch.isalnum())


def _build_library_entry_index(entries: list[LibraryEntry]) -> _LibraryEntryIndex:
    features: list[_LibraryEntryFeature] = []
    by_norm_mpn: dict[str, list[_LibraryEntryFeature]] = defaultdict(list)
    by_norm_device_name: dict[str, list[_LibraryEntryFeature]] = defaultdict(list)
    by_norm_alias: dict[str, list[_LibraryEntryFeature]] = defaultdict(list)
    by_part_number_key: dict[str, list[_LibraryEntryFeature]] = defaultdict(list)
    by_norm_component_class: dict[str, list[_LibraryEntryFeature]] = defaultdict(list)

    for entry in entries:
        norm_device_name = _norm(entry.device_name)
        norm_package_name = _norm(entry.package_name)
        norm_component_class = _norm(entry.component_class)
        norm_mpn = _norm(entry.mpn)
        norm_aliases = tuple(
            alias
            for alias in (_norm(item) for item in entry.aliases)
            if alias
        )
        package_variants = frozenset(_package_variants(entry.package_name))
        part_number_keys = frozenset(_entry_part_number_keys(entry))
        mpn_part_number_keys = frozenset(_part_number_variants(str(entry.mpn or "")))

        feature = _LibraryEntryFeature(
            entry=entry,
            norm_device_name=norm_device_name,
            norm_package_name=norm_package_name,
            norm_component_class=norm_component_class,
            norm_mpn=norm_mpn,
            norm_aliases=norm_aliases,
            package_variants=package_variants,
            part_number_keys=part_number_keys,
            mpn_part_number_keys=mpn_part_number_keys,
        )
        features.append(feature)

        if norm_mpn:
            by_norm_mpn[norm_mpn].append(feature)
        if norm_device_name:
            by_norm_device_name[norm_device_name].append(feature)
        for alias in norm_aliases:
            by_norm_alias[alias].append(feature)
        for key in part_number_keys:
            by_part_number_key[key].append(feature)
        by_norm_component_class[norm_component_class].append(feature)

    return _LibraryEntryIndex(
        features=features,
        by_norm_mpn=dict(by_norm_mpn),
        by_norm_device_name=dict(by_norm_device_name),
        by_norm_alias=dict(by_norm_alias),
        by_part_number_key=dict(by_part_number_key),
        by_norm_component_class=dict(by_norm_component_class),
    )


def _package_variants(value: str | None) -> set[str]:
    base = _norm(value)
    if not base:
        return set()

    variants = {base}

    codes = ("0201", "0402", "0603", "0805", "1206", "1210", "2512")
    for code in codes:
        if code in base:
            variants.add(code)

    # Common prefixed patterns like R0603/C0603.
    if len(base) >= 5 and base[0] in {"R", "C", "L"} and base[1:5].isdigit():
        variants.add(base[1:5])
    if len(base) >= 5 and base[0] in {"R", "C", "L"} and base[1] == "O" and base[2:5].isdigit():
        variants.add(f"0{base[2:5]}")

    normalized = str(value or "").upper()
    patterns = (
        r"(SOT[-_ ]?23(?:[-_ ]?\d+)?)",
        r"(SOT[-_ ]?223)",
        r"(SOT[-_ ]?89)",
        r"(HSOP[-_ ]?\d+)",
        r"(HSSOP[-_ ]?\d+)",
        r"(SOP[-_ ]?\d+)",
        r"(SOD[-_ ]?\d+)",
        r"(DO[-_ ]?214[A-Z]*)",
        r"(SMA)",
        r"(SMB)",
        r"(SMC)",
        r"(MSOP[-_ ]?\d+)",
        r"(SSOP[-_ ]?\d+)",
        r"(TSOP[-_ ]?\d+)",
        r"(SOIC[-_ ]?\d+)",
        r"(TSSOP[-_ ]?\d+)",
        r"(QFN[-_ ]?\d+)",
        r"(LQFP[-_ ]?\d+)",
        r"(QFP[-_ ]?\d+)",
        r"(DIP[-_ ]?\d+)",
        r"(TO[-_ ]?92)",
        r"(TO[-_ ]?220)",
    )
    for pattern in patterns:
        match = re.search(pattern, normalized)
        if match:
            variants.add(_norm(match.group(1)))

    family_token = _package_family_token(base)
    if family_token:
        variants.add(family_token)

    pitch = _extract_pitch_mm(value)
    if pitch is not None:
        variants.add(f"PITCH{int(round(pitch * 100)):04d}")

    pin_count = _extract_pin_count(value)
    if pin_count is not None:
        variants.add(f"PINCOUNT{pin_count}")

    return variants


def _component_class(component: Component) -> str:
    explicit = component.attributes.get("component_class")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip().lower()

    hint_blob = " ".join(_component_package_hints(component)).upper()
    ref = component.refdes.upper()
    if any(token in hint_blob for token in ("MOSFET", "PMOS", "NMOS")):
        return "mosfet"
    if any(token in hint_blob for token in ("TRANSISTOR", "BJT", "NPN", "PNP")):
        return "transistor"
    if re.search(r"\b2N7002\b|\bSI\d{4}[A-Z0-9\-]*\b|\bAO\d{4}[A-Z0-9\-]*\b|\bBSS\d+\b|\bFDN\d+\b", hint_blob):
        return "mosfet"
    if re.search(r"\bS8050\b|\bS8550\b|\bMMBT\d+\b|\b2N\d+\b|\bBC\d+\b", hint_blob):
        return "transistor"
    if any(token in hint_blob for token in ("DIODE", "TVS", "SCHOTTKY", "SOD-", "DO-214", "SMA", "SMB", "SMC")):
        return "diode"
    if "SCREWTERMINAL" in hint_blob or "TERMINAL" in hint_blob:
        return "connector"
    if any(token in hint_blob for token in ("CONN", "HEADER", "JST", "PINHDR")):
        return "connector"
    if "RES-ARRAY" in hint_blob or "RESISTORARRAY" in hint_blob:
        return "resistor_array"
    if ref.startswith("LED"):
        return "led"
    if ref.startswith("R"):
        return "resistor"
    if ref.startswith("C"):
        return "capacitor"
    if ref.startswith("L"):
        return "inductor"
    if ref.startswith("D"):
        return "diode"
    if ref.startswith("FB"):
        return "ferrite"
    if ref.startswith(("J", "CN", "HDR")):
        return "connector"
    if ref.startswith("U"):
        return "ic"
    return "generic"


def _component_package_keys(component: Component) -> set[str]:
    keys: set[str] = set()
    for raw in _component_package_hints(component):
        normalized = _norm(raw)
        if normalized:
            keys.add(normalized)
    return keys


def _component_package_variants(component: Component) -> set[str]:
    variants: set[str] = set()
    for raw in _component_package_hints(component):
        variants.update(_package_variants(raw))
    return variants


def _component_package_hints(component: Component) -> list[str]:
    hints: list[str] = []
    for raw in (
        component.package_id,
        component.source_name,
        component.attributes.get("Footprint"),
        component.attributes.get("Package"),
        component.attributes.get("Name"),
        component.attributes.get("footprint"),
        component.attributes.get("package"),
        component.attributes.get("package_name"),
    ):
        text = str(raw or "").strip()
        if text:
            hints.append(text)
    return hints


def _component_part_number_keys(component: Component) -> set[str]:
    keys: set[str] = set()
    for value in _component_part_number_raw_values(component):
        split_parts, _ = _split_combined_part_number_and_package(value)
        for split_part in split_parts:
            keys.update(_part_number_variants(split_part))

        if not _looks_like_part_number(value):
            continue
        keys.update(_part_number_variants(value))
    return keys


def _entry_part_number_keys(entry: LibraryEntry) -> set[str]:
    keys: set[str] = set()
    for raw in (entry.mpn, entry.device_name, *entry.aliases):
        text = str(raw or "").strip()
        if not text:
            continue
        if not _looks_like_part_number(text):
            continue
        keys.update(_part_number_variants(text))
    return keys


def _component_part_number_raw_values(component: Component) -> list[str]:
    raw_values: list[str] = []
    for raw in (
        component.mpn,
        component.attributes.get("MPN"),
        component.attributes.get("Manufacturer Part"),
        component.attributes.get("Manufacturer Part Number"),
        component.attributes.get("Mfr Part Number"),
        component.attributes.get("Part Number"),
        component.attributes.get("part_number"),
        component.attributes.get("manufacturer_part"),
        component.source_name,
        component.attributes.get("Name"),
        component.attributes.get("Device"),
    ):
        text = str(raw or "").strip()
        if text:
            raw_values.append(text)
    return raw_values


def _non_passive_part_match_package_requirements(component: Component) -> tuple[set[str], set[str]]:
    hints: list[str] = []
    for raw in (
        component.package_id,
        component.attributes.get("Footprint"),
        component.attributes.get("Package"),
        component.attributes.get("footprint"),
        component.attributes.get("package"),
        component.attributes.get("package_name"),
    ):
        text = str(raw or "").strip()
        if text:
            hints.append(text)

    for value in _component_part_number_raw_values(component):
        _, extracted_packages = _split_combined_part_number_and_package(value)
        hints.extend(sorted(extracted_packages))

    package_keys: set[str] = set()
    package_variants: set[str] = set()
    for hint in hints:
        normalized = _norm(hint)
        if normalized:
            package_keys.add(normalized)
        package_variants.update(_package_variants(hint))
    return package_keys, package_variants


def _part_number_match_score(
    component_parts: set[str],
    entry: LibraryEntry,
    package_keys: set[str],
    package_variants: set[str],
) -> int:
    entry_parts = _entry_part_number_keys(entry)
    if not entry_parts:
        return 0

    common = component_parts.intersection(entry_parts)
    if not common:
        return 0

    score = 0
    longest = max(len(token) for token in common)
    if longest >= 12:
        score += 4
    elif longest >= 8:
        score += 3
    else:
        score += 2

    if entry.mpn and _part_number_variants(entry.mpn).intersection(component_parts):
        score += 3

    if package_keys:
        entry_pkg = _norm(entry.package_name)
        if not _package_compatible(entry_pkg, _package_variants(entry.package_name), package_keys, package_variants):
            return 0
        score += 2

    if entry.add_token:
        score += 1

    return score


def _part_number_match_score_from_feature(
    component_parts: set[str],
    feature: _LibraryEntryFeature,
    package_keys: set[str],
    package_variants: set[str],
) -> int:
    common = component_parts.intersection(feature.part_number_keys)
    if not common:
        return 0

    score = 0
    longest = max(len(token) for token in common)
    if longest >= 12:
        score += 4
    elif longest >= 8:
        score += 3
    else:
        score += 2

    if feature.norm_mpn and feature.mpn_part_number_keys.intersection(component_parts):
        score += 3

    if package_keys:
        if not _package_compatible(
            feature.norm_package_name,
            set(feature.package_variants),
            package_keys,
            package_variants,
        ):
            return 0
        score += 2

    if feature.entry.add_token:
        score += 1

    return score


def _part_number_variants(value: str) -> set[str]:
    text = str(value or "").strip().upper()
    if not text:
        return set()

    variants: set[str] = set()
    normalized = _norm(text)
    if normalized:
        variants.add(normalized)

    stripped = re.sub(r"(?:/REEL|/TR|TR|T/R)$", "", text)
    stripped = stripped.strip("-_ ")
    normalized_stripped = _norm(stripped)
    if normalized_stripped:
        variants.add(normalized_stripped)

    for token in re.split(r"[/_,;:\s\-\(\)\+]+", text):
        token_norm = _norm(token)
        if len(token_norm) >= 6:
            variants.add(token_norm)
            variants.update(_reduced_part_number_variants(token_norm))

    variants.update(_reduced_part_number_variants(normalized))

    return variants


def _reduced_part_number_variants(normalized: str) -> set[str]:
    variants: set[str] = set()
    token = str(normalized or "").strip().upper()
    if not token:
        return variants

    # Keep near-exact forms by trimming one trailing alpha package/order code.
    if token[-1:].isalpha():
        one_step = token[:-1]
        if one_step and _looks_like_part_number(one_step):
            variants.add(one_step)

    # Include base core by trimming a trailing alpha suffix block (e.g.
    # LMR14020SDDAR -> LMR14020, S8050MD -> S8050).
    trimmed_all = re.sub(r"[A-Z]+$", "", token)
    if trimmed_all and trimmed_all != token and _looks_like_part_number(trimmed_all):
        variants.add(trimmed_all)

    return variants


def _split_combined_part_number_and_package(value: str) -> tuple[set[str], set[str]]:
    text = str(value or "").strip().upper()
    if not text:
        return set(), set()

    normalized = _norm(text)
    if not normalized:
        return set(), set()

    package_tokens = sorted(
        {
            token
            for token in _package_variants(text)
            if _is_structured_package_variant(token)
        },
        key=len,
        reverse=True,
    )
    if not package_tokens:
        return set(), set()

    part_tokens: set[str] = set()
    package_hints: set[str] = set(package_tokens)
    for package_token in package_tokens:
        if normalized.endswith(package_token) and len(normalized) - len(package_token) >= 5:
            part = normalized[: -len(package_token)]
            if _looks_like_part_number(part):
                part_tokens.add(part)
        if normalized.startswith(package_token) and len(normalized) - len(package_token) >= 5:
            part = normalized[len(package_token) :]
            if _looks_like_part_number(part):
                part_tokens.add(part)
    return part_tokens, package_hints


def _looks_like_part_number(value: str) -> bool:
    text = str(value or "").strip().upper()
    if not text:
        return False
    if len(text) < 5:
        return False

    norm = _norm(text)
    if not norm:
        return False

    has_letter = any(ch.isalpha() for ch in norm)
    has_digit = any(ch.isdigit() for ch in norm)
    if not (has_letter and has_digit):
        return False

    if norm in {"R0603", "R0805", "C0603", "C0805", "L0603"}:
        return False
    if re.fullmatch(r"[RCLD]?\d{4}", norm):
        return False
    return True


def _is_structured_package_variant(token: str) -> bool:
    text = _norm(token)
    if not text:
        return False
    return bool(
        re.fullmatch(
            r"(SOT\d+|SOD\d+|DO214[A-Z]*|SMA|SMB|SMC|HSOP\d+|HSSOP\d+|SOP\d+|MSOP\d+|SSOP\d+|TSOP\d+|SOIC\d+|TSSOP\d+|QFN\d+|LQFP\d+|QFP\d+|DIP\d+|TO92|TO220|DFN\d+|TQFN\d+|BGA\d+)",
            text,
        )
    )


def _prefer_stage_candidate(
    component: Component,
    candidates: list[LibraryEntry],
    stage_name: str,
) -> LibraryEntry | None:
    if stage_name != "stage1_part_number":
        return None

    if _component_class(component) in {"resistor", "capacitor"}:
        return None

    component_parts = _component_part_number_keys(component)
    package_keys, component_variants = _non_passive_part_match_package_requirements(component)
    if not component_parts:
        return None
    if not package_keys and not component_variants:
        return None

    ranked = sorted(
        candidates,
        key=lambda entry: (
            -_part_number_match_score(
                component_parts,
                entry,
                package_keys,
                component_variants,
            ),
            -int(bool(entry.add_token)),
            _norm(entry.library_name or ""),
            _norm(entry.device_name),
        ),
    )
    if not ranked:
        return None

    if len(ranked) == 1:
        return ranked[0]

    top_score = _part_number_match_score(component_parts, ranked[0], package_keys, component_variants)
    second_score = _part_number_match_score(component_parts, ranked[1], package_keys, component_variants)
    # For strong part-number+package matches, prefer deterministic best candidate
    # instead of forcing ambiguity fallback.
    if top_score >= 5:
        return ranked[0]
    if top_score > second_score:
        return ranked[0]
    return None


def _prefer_passive_external_candidate(
    component: Component,
    candidates: list[LibraryEntry],
    preferred_paths: set[str] | None = None,
) -> LibraryEntry | None:
    class_name = _component_class(component)
    if class_name not in {"resistor", "capacitor"}:
        return None

    size_codes = {"0201", "0402", "0603", "0805", "1206", "1210", "2512"}
    package_variants = _component_package_variants(component)
    if not package_variants.intersection(size_codes):
        return None

    normalized_preferred = _normalize_path_set(preferred_paths or set())
    ranked = sorted(
        candidates,
        key=lambda entry: _passive_sort_key(component, entry, normalized_preferred),
    )
    if not ranked:
        return None

    top = ranked[0]
    if not top.add_token:
        return None
    return top


def _passive_sort_key(
    component: Component,
    entry: LibraryEntry,
    preferred_paths: set[str],
) -> tuple[int, int, int, int, int, int, str, str, str]:
    class_name = _component_class(component)
    component_variants = _component_package_variants(component)
    entry_package = _norm(entry.package_name)
    entry_variants = _package_variants(entry.package_name)

    package_score = 0
    if entry_package and entry_package in component_variants:
        package_score = 3
    elif component_variants.intersection(entry_variants):
        package_score = 2
    elif entry_package and any(
        variant and (variant in entry_package or entry_package in variant)
        for variant in component_variants
    ):
        package_score = 1

    class_score = 0
    if _norm(entry.component_class) == _norm(class_name):
        class_score = 2
    elif not entry.component_class:
        class_score = 1

    external_score = 1 if entry.add_token else 0
    preferred_library_score = 1 if _entry_library_path_key(entry) in preferred_paths else 0
    library_score = _passive_library_score(class_name, entry)
    symbol_score = _passive_symbol_score(class_name, entry)

    # Sort ascending with negative priority scores for deterministic "best first".
    return (
        -external_score,
        -preferred_library_score,
        -package_score,
        -class_score,
        -library_score,
        -symbol_score,
        _norm(entry.library_name or ""),
        _norm(entry.device_name),
        _norm(entry.add_token or ""),
    )


def _passive_library_score(class_name: str, entry: LibraryEntry) -> int:
    token = _norm(entry.library_name or "")
    if not token and entry.add_token and ":" in entry.add_token:
        token = _norm(entry.add_token.split(":", 1)[0])

    score = 0
    if "RCL" in token:
        score += 20

    if class_name == "resistor":
        if "RESISTOR" in token:
            score += 10
        if token.startswith("R"):
            score += 2
    elif class_name == "capacitor":
        if "CAPACITOR" in token:
            score += 10
        if token.startswith("C"):
            score += 2

    if "SPARKFUN" in token:
        score += 1
    return score


def _entry_library_path_key(entry: LibraryEntry) -> str:
    raw = str(entry.library_path or "").strip()
    if not raw:
        return ""
    try:
        return str(Path(raw).expanduser().resolve())
    except Exception:
        return raw


def _normalize_path_set(values: set[str]) -> set[str]:
    normalized: set[str] = set()
    for item in values:
        raw = str(item or "").strip()
        if not raw:
            continue
        try:
            normalized.add(str(Path(raw).expanduser().resolve()))
        except Exception:
            normalized.add(raw)
    return normalized


def _normalize_library_preference_paths(
    values: dict[str, set[str]],
) -> dict[str, set[str]]:
    normalized: dict[str, set[str]] = {}
    for class_name, paths in values.items():
        key = _norm(class_name)
        if not key:
            continue
        normalized_class = ""
        if key == "RESISTOR":
            normalized_class = "resistor"
        elif key == "CAPACITOR":
            normalized_class = "capacitor"
        if not normalized_class:
            continue
        normalized[normalized_class] = _normalize_path_set(paths)
    return normalized


def _passive_symbol_score(class_name: str, entry: LibraryEntry) -> int:
    token = _norm(f"{entry.device_name} {entry.symbol_name}")
    if class_name == "resistor":
        if "RUS" in token:
            return 4
        if "REU" in token:
            return 3
        if token.startswith("R"):
            return 2
    if class_name == "capacitor":
        if "CUS" in token:
            return 4
        if "CEU" in token:
            return 3
        if token.startswith("C"):
            return 2
    return 0


def _is_screw_terminal_component(component: Component) -> bool:
    blob = " ".join(_component_package_hints(component)).upper()
    keywords = (
        "SCREWTERMINAL",
        "TERMINAL",
        "TERMINALBLOCK",
        "WJ381",
        "XY2500",
        "KF",
    )
    return any(token in blob for token in keywords)


def _stage2_screw_terminal(component: Component, entries: list[LibraryEntry]) -> list[LibraryEntry]:
    comp_pitch = _component_pitch_mm(component)
    comp_pin_count = _component_pin_count(component)
    package_keys = _component_package_keys(component)
    ranked: list[tuple[int, LibraryEntry]] = []
    for entry in entries:
        entry_blob = _entry_text_blob(entry)
        entry_class = _norm(entry.component_class)
        if entry_class not in {"", "CONNECTOR"}:
            continue

        entry_pin_count = _entry_pin_count(entry)
        if comp_pin_count is not None and entry_pin_count is not None and comp_pin_count != entry_pin_count:
            continue

        entry_pitch = _entry_pitch_mm(entry)
        if (
            comp_pitch is not None
            and entry_pitch is not None
            and abs(comp_pitch - entry_pitch) > 0.35
        ):
            continue

        looks_like_terminal = any(
            token in entry_blob
            for token in ("SCREW", "TERMINAL", "WJ381", "XY2500", "TBLOCK", "KF")
        )
        if not looks_like_terminal and entry_pin_count is None and entry_pitch is None:
            continue

        score = 0
        if looks_like_terminal:
            score += 6
        if entry.add_token:
            score += 3
        entry_pkg_norm = _norm(entry.package_name)
        if entry_pkg_norm and entry_pkg_norm in package_keys:
            score += 8
        elif entry_pkg_norm and any(
            key and (key in entry_pkg_norm or entry_pkg_norm in key)
            for key in package_keys
        ):
            score += 2
        if comp_pin_count is not None and entry_pin_count is not None:
            score += 6 if comp_pin_count == entry_pin_count else 0
        elif comp_pin_count is not None and entry_pin_count is None:
            score += 1
        if comp_pitch is not None and entry_pitch is not None:
            delta = abs(comp_pitch - entry_pitch)
            if delta <= 0.05:
                score += 6
            elif delta <= 0.12:
                score += 5
            elif delta <= 0.20:
                score += 4
            elif delta <= 0.30:
                score += 3
        elif comp_pitch is not None and entry_pitch is None:
            score += 1

        ranked.append((score, entry))

    if not ranked:
        return []

    best = max(score for score, _ in ranked)
    best_entries = [entry for score, entry in ranked if score == best]
    if len(best_entries) <= 1:
        return best_entries

    signatures = {
        (
            _norm(entry.device_name),
            _norm(entry.package_name),
            _norm(entry.symbol_name),
        )
        for entry in best_entries
    }
    if len(signatures) == 1:
        # Equivalent candidates from multiple sources should not force a new
        # generated part. Pick one deterministically.
        chosen = sorted(
            best_entries,
            key=lambda entry: (
                _norm(entry.library_name or ""),
                _norm(entry.device_name),
                _norm(entry.package_name),
                _norm(entry.add_token),
            ),
        )[0]
        return [chosen]
    return best_entries


def _dedupe_candidate_entries(entries: list[LibraryEntry]) -> list[LibraryEntry]:
    unique: list[LibraryEntry] = []
    seen: set[tuple[str, str, str, str]] = set()
    for entry in entries:
        key = (
            _norm(entry.add_token or entry.device_name),
            _norm(entry.package_name),
            _norm(entry.library_name or ""),
            _norm(entry.device_name),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)
    return unique


def _entry_text_blob(entry: LibraryEntry) -> str:
    parts = [
        entry.device_name,
        entry.package_name,
        entry.symbol_name,
        entry.library_name,
        *entry.aliases,
    ]
    return " ".join(str(part or "").upper() for part in parts if str(part or "").strip())


def _component_pitch_mm(component: Component) -> float | None:
    values = _component_package_hints(component)
    for value in values:
        pitch = _extract_pitch_mm(value)
        if pitch is not None:
            return pitch
    return None


def _component_pin_count(component: Component) -> int | None:
    values = _component_package_hints(component)
    for value in values:
        count = _extract_pin_count(value)
        if count is not None:
            return count
    return None


def _entry_pitch_mm(entry: LibraryEntry) -> float | None:
    for value in (entry.package_name, entry.device_name, entry.symbol_name, *entry.aliases):
        pitch = _extract_pitch_mm(value)
        if pitch is not None:
            return pitch
    return None


def _entry_pin_count(entry: LibraryEntry) -> int | None:
    for value in (entry.package_name, entry.device_name, entry.symbol_name, *entry.aliases):
        count = _extract_pin_count(value)
        if count is not None:
            return count
    return None


def _extract_pitch_mm(value: str | None) -> float | None:
    raw = str(value or "").upper()
    if not raw:
        return None
    normalized = re.sub(r"(?<=\d)_(?=\d)", ".", raw)
    normalized = normalized.replace(",", ".")
    patterns = (
        r"(\d+(?:\.\d+)?)\s*MM",
        r"P(?:ITCH)?[-_ ]?(\d+(?:\.\d+)?)",
    )
    for pattern in patterns:
        match = re.search(pattern, normalized)
        if not match:
            continue
        try:
            pitch = float(match.group(1))
        except Exception:
            continue
        if 0.2 <= pitch <= 10.0:
            return pitch

    encoded_pitch = re.search(
        r"(?:QFN|TQFN|DFN|SOIC|SOP|HSOP|HSSOP|MSOP|SSOP|TSSOP|TSOP|LQFP|QFP)(\d{2,4})P",
        raw,
    )
    if encoded_pitch:
        try:
            encoded = int(encoded_pitch.group(1))
            pitch = float(encoded) / 100.0
            if 0.2 <= pitch <= 10.0:
                return pitch
        except Exception:
            return None
    return None


def _extract_pin_count(value: str | None) -> int | None:
    raw = str(value or "").upper()
    if not raw:
        return None
    patterns = (
        r"(?:QFN|TQFN|DFN|SOIC|SOP|HSOP|HSSOP|MSOP|SSOP|TSSOP|TSOP|LQFP|QFP)[-_ ]?(\d{1,3})",
        r"SCREWTERMINAL[-_ ]?(?:\d+(?:\.\d+)?MM[-_ ]?)?(\d{1,2})(?:[^0-9]|$)",
        r"CONN[_-]?0?(\d{1,2})(?=[._-]?\d+(?:\.\d+)?MM)",
        r"1X(\d{1,2})",
        r"(?:^|[^0-9])(\d{1,2})P(?:[^A-Z0-9]|$)",
        r"S(\d{1,2})B",
        r"(?:^|[^0-9])PIN[-_ ]?(\d{1,2})(?:[^0-9]|$)",
        r"-(\d{1,2})N(?:[^A-Z0-9]|$)",
        r"[-_](\d{1,2})$",
    )
    for pattern in patterns:
        match = re.search(pattern, raw)
        if not match:
            continue
        try:
            count = int(match.group(1))
        except Exception:
            continue
        if 2 <= count <= 64:
            return count
    return None


def _is_safe_external_add_token(token: str) -> bool:
    # Wildcard tokens tend to fail scripted ADD in Fusion/EAGLE.
    return "?" not in token and "*" not in token


def _package_compatible(
    entry_pkg: str,
    entry_variants: set[str],
    package_keys: set[str],
    package_variants: set[str],
) -> bool:
    if not package_keys and not package_variants:
        return True
    if entry_pkg and entry_pkg in package_keys:
        return True
    if package_variants and entry_variants and package_variants.intersection(entry_variants):
        return True
    if entry_pkg and package_keys:
        if any(key and (key in entry_pkg or entry_pkg in key) for key in package_keys):
            return True

    entry_families = _variant_family_tokens(entry_variants)
    component_families = _variant_family_tokens(package_variants)
    if entry_families and component_families and entry_families.intersection(component_families):
        entry_pitches = _variant_pitch_tokens(entry_variants)
        component_pitches = _variant_pitch_tokens(package_variants)
        entry_pins = _variant_pin_count_tokens(entry_variants)
        component_pins = _variant_pin_count_tokens(package_variants)

        has_dimension_signal = bool((entry_pitches and component_pitches) or (entry_pins and component_pins))
        if not has_dimension_signal:
            return False

        pitch_ok = True
        if entry_pitches and component_pitches:
            pitch_ok = min(
                abs(left - right)
                for left in entry_pitches
                for right in component_pitches
            ) <= 15  # 0.15 mm in hundredths

        pin_ok = True
        if entry_pins and component_pins:
            pin_ok = min(
                abs(left - right)
                for left in entry_pins
                for right in component_pins
            ) <= 1  # allow exposed-pad +1 count delta

        if pitch_ok and pin_ok:
            return True
    return False


def _package_family_token(base: str) -> str:
    token = _norm(base)
    if any(item in token for item in ("HSOP", "HSSOP", "SOIC", "SOP", "SSOP", "TSSOP", "MSOP", "TSOP")):
        return "FAM_GULLWING"
    if any(item in token for item in ("QFN", "TQFN", "DFN")):
        return "FAM_QFN"
    if any(item in token for item in ("LQFP", "QFP")):
        return "FAM_QFP"
    if "BGA" in token:
        return "FAM_BGA"
    return ""


def _variant_family_tokens(variants: set[str]) -> set[str]:
    return {item for item in variants if item.startswith("FAM_")}


def _variant_pitch_tokens(variants: set[str]) -> set[int]:
    out: set[int] = set()
    for item in variants:
        if not item.startswith("PITCH"):
            continue
        try:
            out.add(int(item.removeprefix("PITCH")))
        except Exception:
            continue
    return out


def _variant_pin_count_tokens(variants: set[str]) -> set[int]:
    out: set[int] = set()
    for item in variants:
        if not item.startswith("PINCOUNT"):
            continue
        try:
            out.add(int(item.removeprefix("PINCOUNT")))
        except Exception:
            continue
    return out


def _resolve_component_package(component: Component, package_lookup: dict[str, Package]) -> Package | None:
    return _shared_resolve_component_package(component, package_lookup)


def _external_package_match_is_compatible(
    component: Component,
    selected: LibraryEntry,
    source_package: Package | None,
    cache: dict[tuple[str, str], dict[str, _ExternalPadGeometry] | None],
    strict_board_fidelity: bool = False,
) -> tuple[bool, str | None]:
    if source_package is None:
        return True, None
    if not selected.library_path or not selected.package_name:
        return True, None

    external_geometry = _load_external_package_geometry(
        library_path=selected.library_path,
        package_name=selected.package_name,
        cache=cache,
    )
    if external_geometry is None:
        return False, "external_package_geometry_unavailable"

    class_name = _component_class(component)
    if class_name in {"resistor", "capacitor"} and _package_is_smd(source_package):
        relaxed_ok, relaxed_reason = _passive_package_geometry_is_compatible(
            source_package=source_package,
            external_pads=external_geometry,
        )
        if not strict_board_fidelity:
            _clear_component_external_origin_offset(component)
            return relaxed_ok, relaxed_reason

        strict_ok, strict_reason = _package_geometry_is_compatible(source_package, external_geometry)
        if strict_ok:
            _clear_component_external_origin_offset(component)
            return True, None
        if relaxed_ok and strict_reason and strict_reason.startswith("pad_origin_mismatch:"):
            translation_ok, transform = _package_geometry_translation_is_compatible(
                source_package=source_package,
                external_pads=external_geometry,
            )
            if translation_ok and transform is not None:
                _set_component_external_origin_offset(component, transform)
            return True, f"relaxed_origin_only:{strict_reason}"
        _clear_component_external_origin_offset(component)
        return False, strict_reason or relaxed_reason
    if class_name == "connector" and _is_screw_terminal_component(component):
        screw_ok, screw_reason = _screw_terminal_package_geometry_is_compatible(
            source_package=source_package,
            external_pads=external_geometry,
        )
        if not screw_ok:
            _clear_component_external_origin_offset(component)
            return False, screw_reason

        strict_ok, strict_reason = _package_geometry_is_compatible(source_package, external_geometry)
        if strict_ok:
            _clear_component_external_origin_offset(component)
            return True, None

        if strict_reason and strict_reason.startswith("pad_origin_mismatch:"):
            translation_ok, transform = _package_geometry_translation_is_compatible(
                source_package=source_package,
                external_pads=external_geometry,
            )
            if translation_ok and transform is not None:
                _set_component_external_origin_offset(component, transform)
                return True, f"relaxed_origin_only:{strict_reason}"

        # Without board-fidelity constraints (schematic-only flows), keep the
        # previous screw-terminal behavior if pitch/count/size still align.
        if not strict_board_fidelity:
            _clear_component_external_origin_offset(component)
            return True, screw_reason

        _clear_component_external_origin_offset(component)
        return False, strict_reason or screw_reason
    strict_ok, strict_reason = _package_geometry_is_compatible(source_package, external_geometry)
    if strict_ok:
        _clear_component_external_origin_offset(component)
        return True, None
    if strict_reason and strict_reason.startswith("pad_origin_mismatch:"):
        translation_ok, transform = _package_geometry_translation_is_compatible(
            source_package=source_package,
            external_pads=external_geometry,
        )
        if translation_ok and transform is not None:
            _set_component_external_origin_offset(component, transform)
            return True, f"relaxed_origin_only:{strict_reason}"
    _clear_component_external_origin_offset(component)
    return False, strict_reason


def _load_external_package_geometry(
    library_path: str,
    package_name: str,
    cache: dict[tuple[str, str], dict[str, _ExternalPadGeometry] | None],
) -> dict[str, _ExternalPadGeometry] | None:
    key = (str(library_path).strip(), _norm(package_name))
    if key in cache:
        return cache[key]

    path = Path(str(library_path).strip())
    if not path.exists():
        cache[key] = None
        return None

    root = _parse_external_library_root(path)
    if root is None:
        cache[key] = None
        return None

    package_el = None
    for candidate in root.findall(".//library/packages/package"):
        name = str(candidate.get("name") or "").strip()
        if _norm(name) == _norm(package_name):
            package_el = candidate
            break
    if package_el is None:
        cache[key] = None
        return None

    pads: dict[str, _ExternalPadGeometry] = {}

    for smd in package_el.findall("./smd"):
        name = str(smd.get("name") or "").strip()
        if not name:
            continue
        pads[name] = _ExternalPadGeometry(
            x_mm=_safe_float(smd.get("x")),
            y_mm=_safe_float(smd.get("y")),
            width_mm=max(_safe_float(smd.get("dx")), 0.0),
            height_mm=max(_safe_float(smd.get("dy")), 0.0),
            drill_mm=None,
        )

    for pad in package_el.findall("./pad"):
        name = str(pad.get("name") or "").strip()
        if not name:
            continue
        diameter = max(_safe_float(pad.get("diameter")), 0.0)
        drill = _safe_float(pad.get("drill"))
        drill_mm = drill if drill > 0.0 else None
        shape = str(pad.get("shape") or "").strip().lower()
        rotation_deg = _rotation_from_attr(str(pad.get("rot") or ""))

        width = diameter
        height = diameter
        if shape == "long" and diameter > 0.0:
            long_span = diameter * 2.0
            if int(round(rotation_deg)) % 180 == 90:
                width = diameter
                height = long_span
            else:
                width = long_span
                height = diameter

        pads[name] = _ExternalPadGeometry(
            x_mm=_safe_float(pad.get("x")),
            y_mm=_safe_float(pad.get("y")),
            width_mm=width,
            height_mm=height,
            drill_mm=drill_mm,
        )

    cache[key] = pads if pads else None
    return cache[key]


def _parse_external_library_root(path: Path) -> ET.Element | None:
    try:
        return ET.parse(path).getroot()
    except Exception:
        pass

    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None

    sanitized = _sanitize_xml_named_entities(raw)
    try:
        return ET.fromstring(sanitized)
    except Exception:
        return None


def _sanitize_xml_named_entities(text: str) -> str:
    xml_builtins = {"amp", "lt", "gt", "apos", "quot"}
    pattern = re.compile(r"&([A-Za-z][A-Za-z0-9]+);")

    def _replace(match: re.Match[str]) -> str:
        name = str(match.group(1) or "")
        if name in xml_builtins:
            return match.group(0)
        decoded = html.unescape(match.group(0))
        if decoded == match.group(0):
            return ""
        return decoded

    return pattern.sub(_replace, text)


def _package_geometry_is_compatible(
    source_package: Package,
    external_pads: dict[str, _ExternalPadGeometry],
) -> tuple[bool, str | None]:
    source_map: dict[str, tuple[float, float, float, float, float | None]] = {}
    for pad in source_package.pads:
        pad_name = str(pad.pad_number or "").strip()
        if not pad_name:
            continue
        source_map[pad_name] = (
            float(pad.at.x_mm),
            float(pad.at.y_mm),
            max(float(pad.width_mm), 0.0),
            max(float(pad.height_mm), 0.0),
            float(pad.drill_mm) if pad.drill_mm is not None and pad.drill_mm > 0.0 else None,
        )

    common = sorted(set(source_map).intersection(set(external_pads)))
    if not common:
        return False, "no_common_pad_names"

    # Require local pad coordinates to line up with source geometry. This prevents
    # origin/rotation mismatches that move footprints away from EasyEDA placements.
    coordinate_tol_mm = 0.02
    size_tol_mm = 0.20
    drill_tol_mm = 0.10
    distance_tol_mm = 0.05

    for pad_name in common:
        src_x, src_y, src_w, src_h, src_drill = source_map[pad_name]
        ext = external_pads[pad_name]
        if abs(src_x - ext.x_mm) > coordinate_tol_mm or abs(src_y - ext.y_mm) > coordinate_tol_mm:
            return False, f"pad_origin_mismatch:{pad_name}"

        src_plated = src_drill is not None and src_drill > 0.0
        ext_plated = ext.drill_mm is not None and ext.drill_mm > 0.0
        if src_plated != ext_plated:
            return False, f"pad_plating_mismatch:{pad_name}"
        if src_plated and ext.drill_mm is not None and src_drill is not None:
            if abs(src_drill - ext.drill_mm) > drill_tol_mm:
                return False, f"pad_drill_mismatch:{pad_name}"

        if src_w > 0.0 and src_h > 0.0 and ext.width_mm > 0.0 and ext.height_mm > 0.0:
            src_major, src_minor = max(src_w, src_h), min(src_w, src_h)
            ext_major, ext_minor = max(ext.width_mm, ext.height_mm), min(ext.width_mm, ext.height_mm)
            if abs(src_major - ext_major) > size_tol_mm or abs(src_minor - ext_minor) > size_tol_mm:
                return False, f"pad_size_mismatch:{pad_name}"

    if len(common) >= 2:
        for idx in range(len(common)):
            left_name = common[idx]
            lx, ly, _, _, _ = source_map[left_name]
            ex_l = external_pads[left_name]
            for jdx in range(idx + 1, len(common)):
                right_name = common[jdx]
                rx, ry, _, _, _ = source_map[right_name]
                ex_r = external_pads[right_name]
                src_dist = math.hypot(lx - rx, ly - ry)
                ext_dist = math.hypot(ex_l.x_mm - ex_r.x_mm, ex_l.y_mm - ex_r.y_mm)
                if abs(src_dist - ext_dist) > distance_tol_mm:
                    return False, "pad_pitch_mismatch"

    return True, None


def _package_geometry_translation_is_compatible(
    source_package: Package,
    external_pads: dict[str, _ExternalPadGeometry],
) -> tuple[bool, _ExternalGeometryTransform | None]:
    source_map: dict[str, tuple[float, float, float, float, float | None]] = {}
    for pad in source_package.pads:
        pad_name = str(pad.pad_number or "").strip()
        if not pad_name:
            continue
        source_map[pad_name] = (
            float(pad.at.x_mm),
            float(pad.at.y_mm),
            max(float(pad.width_mm), 0.0),
            max(float(pad.height_mm), 0.0),
            float(pad.drill_mm) if pad.drill_mm is not None and pad.drill_mm > 0.0 else None,
        )

    common = sorted(set(source_map).intersection(set(external_pads)))
    if len(common) < 2:
        return False, None

    size_tol_mm = 0.60
    drill_tol_mm = 0.12
    distance_tol_mm = 0.25
    translation_tol_mm = 0.15

    for pad_name in common:
        src_w = source_map[pad_name][2]
        src_h = source_map[pad_name][3]
        src_drill = source_map[pad_name][4]
        ext = external_pads[pad_name]

        src_plated = src_drill is not None and src_drill > 0.0
        ext_plated = ext.drill_mm is not None and ext.drill_mm > 0.0
        if src_plated != ext_plated:
            return False, None
        if src_plated and ext.drill_mm is not None and src_drill is not None:
            if abs(src_drill - ext.drill_mm) > drill_tol_mm:
                return False, None

        if src_w > 0.0 and src_h > 0.0 and ext.width_mm > 0.0 and ext.height_mm > 0.0:
            src_major, src_minor = max(src_w, src_h), min(src_w, src_h)
            ext_major, ext_minor = max(ext.width_mm, ext.height_mm), min(ext.width_mm, ext.height_mm)
            if abs(src_major - ext_major) > size_tol_mm or abs(src_minor - ext_minor) > size_tol_mm:
                return False, None

    best: tuple[tuple[float, int], _ExternalGeometryTransform] | None = None
    for rotation_deg in (0, 90, 180, 270):
        offsets: list[tuple[float, float]] = []
        rotated: dict[str, tuple[float, float]] = {}
        for pad_name in common:
            src_x, src_y, _, _, _ = source_map[pad_name]
            ext = external_pads[pad_name]
            ext_x, ext_y = _rotate_xy(ext.x_mm, ext.y_mm, rotation_deg)
            rotated[pad_name] = (ext_x, ext_y)
            offsets.append((src_x - ext_x, src_y - ext_y))

        avg_dx = sum(item[0] for item in offsets) / float(len(offsets))
        avg_dy = sum(item[1] for item in offsets) / float(len(offsets))
        max_deviation = max(
            max(abs(dx - avg_dx), abs(dy - avg_dy))
            for dx, dy in offsets
        )
        if max_deviation > translation_tol_mm:
            continue

        distance_ok = True
        for idx in range(len(common)):
            left_name = common[idx]
            sx_l, sy_l, _, _, _ = source_map[left_name]
            ex_l, ey_l = rotated[left_name]
            for jdx in range(idx + 1, len(common)):
                right_name = common[jdx]
                sx_r, sy_r, _, _, _ = source_map[right_name]
                ex_r, ey_r = rotated[right_name]
                src_dist = math.hypot(sx_l - sx_r, sy_l - sy_r)
                ext_dist = math.hypot(ex_l - ex_r, ey_l - ey_r)
                if abs(src_dist - ext_dist) > distance_tol_mm:
                    distance_ok = False
                    break
            if not distance_ok:
                break
        if not distance_ok:
            continue

        candidate = _ExternalGeometryTransform(
            offset_x_mm=avg_dx,
            offset_y_mm=avg_dy,
            rotation_deg=int(rotation_deg),
        )
        score = (int(round(max_deviation * 1_000_000.0)), int(rotation_deg))
        if best is None or score < best[0]:
            best = (score, candidate)

    if best is None:
        return False, None
    return True, best[1]


def _rotate_xy(x_mm: float, y_mm: float, rotation_deg: int) -> tuple[float, float]:
    angle = int(rotation_deg) % 360
    if angle == 0:
        return (x_mm, y_mm)
    if angle == 90:
        return (-y_mm, x_mm)
    if angle == 180:
        return (-x_mm, -y_mm)
    if angle == 270:
        return (y_mm, -x_mm)
    radians = math.radians(float(angle))
    cos_a = math.cos(radians)
    sin_a = math.sin(radians)
    return (
        x_mm * cos_a - y_mm * sin_a,
        x_mm * sin_a + y_mm * cos_a,
    )


def _set_component_external_origin_offset(component: Component, transform: _ExternalGeometryTransform) -> None:
    component.attributes["_external_origin_offset_x_mm"] = float(transform.offset_x_mm)
    component.attributes["_external_origin_offset_y_mm"] = float(transform.offset_y_mm)
    rotation = int(transform.rotation_deg) % 360
    if rotation:
        component.attributes["_external_rotation_offset_deg"] = rotation
    else:
        component.attributes.pop("_external_rotation_offset_deg", None)


def _clear_component_external_origin_offset(component: Component) -> None:
    component.attributes.pop("_external_origin_offset_x_mm", None)
    component.attributes.pop("_external_origin_offset_y_mm", None)
    component.attributes.pop("_external_rotation_offset_deg", None)


def _package_is_smd(package: Package) -> bool:
    if not package.pads:
        return False
    for pad in package.pads:
        drill = float(pad.drill_mm) if pad.drill_mm is not None else 0.0
        if drill > 0.0:
            return False
    return True


def _force_local_passive_reason(component: Component, source_package: Package | None) -> str | None:
    class_name = _component_class(component)
    if class_name not in {"resistor", "capacitor"}:
        return None

    if source_package is not None and not _package_is_smd(source_package):
        return "passive_through_hole_source_package"

    hint_blob = " ".join(
        [
            *_component_package_hints(component),
            str(component.attributes.get("3D Model Title") or ""),
            str(component.attributes.get("3d_model_title") or ""),
        ]
    ).upper()
    through_hole_markers = (
        "AXIAL",
        "THROUGHHOLE",
        "THROUGH-HOLE",
        "PTH",
        "RADIAL",
        "-TH",
        "_TH",
        "TH_",
    )
    if any(marker in hint_blob for marker in through_hole_markers):
        return "passive_through_hole_hint"

    return None


def _passive_package_geometry_is_compatible(
    source_package: Package,
    external_pads: dict[str, _ExternalPadGeometry],
) -> tuple[bool, str | None]:
    source_pads = [
        pad
        for pad in source_package.pads
        if str(pad.pad_number or "").strip()
    ]
    if len(source_pads) < 2:
        return False, "insufficient_source_pads"
    if not _package_is_smd(source_package):
        return False, "source_package_not_smd"

    source_by_name = {
        str(pad.pad_number).strip(): pad
        for pad in source_pads
    }
    common_names = sorted(set(source_by_name).intersection(set(external_pads)))
    if len(common_names) >= 2:
        chosen = common_names[:2]
    else:
        ordered_source = sorted(source_by_name.items(), key=lambda item: _pin_sort_key(item[0]))
        ordered_external = sorted(external_pads.items(), key=lambda item: _pin_sort_key(item[0]))
        if len(ordered_source) < 2 or len(ordered_external) < 2:
            return False, "insufficient_external_pads"
        chosen = [ordered_source[0][0], ordered_source[1][0]]
        external_map = {chosen[0]: ordered_external[0][1], chosen[1]: ordered_external[1][1]}
    if len(common_names) >= 2:
        external_map = {name: external_pads[name] for name in chosen}

    src0 = source_by_name[chosen[0]]
    src1 = source_by_name[chosen[1]]
    ext0 = external_map[chosen[0]]
    ext1 = external_map[chosen[1]]

    size_tol_mm = 0.45
    pitch_tol_mm = 0.60

    if not _pad_size_close(src0.width_mm, src0.height_mm, ext0.width_mm, ext0.height_mm, size_tol_mm):
        return False, f"pad_size_mismatch:{chosen[0]}"
    if not _pad_size_close(src1.width_mm, src1.height_mm, ext1.width_mm, ext1.height_mm, size_tol_mm):
        return False, f"pad_size_mismatch:{chosen[1]}"

    src_pitch = math.hypot(float(src0.at.x_mm) - float(src1.at.x_mm), float(src0.at.y_mm) - float(src1.at.y_mm))
    ext_pitch = math.hypot(float(ext0.x_mm) - float(ext1.x_mm), float(ext0.y_mm) - float(ext1.y_mm))
    if abs(src_pitch - ext_pitch) > pitch_tol_mm:
        return False, "pad_pitch_mismatch"

    return True, None


def _pad_size_close(
    src_w: float,
    src_h: float,
    ext_w: float,
    ext_h: float,
    tol_mm: float,
) -> bool:
    src_major, src_minor = max(float(src_w), float(src_h)), min(float(src_w), float(src_h))
    ext_major, ext_minor = max(float(ext_w), float(ext_h)), min(float(ext_w), float(ext_h))
    return abs(src_major - ext_major) <= tol_mm and abs(src_minor - ext_minor) <= tol_mm


def _screw_terminal_package_geometry_is_compatible(
    source_package: Package,
    external_pads: dict[str, _ExternalPadGeometry],
) -> tuple[bool, str | None]:
    source_by_name = {
        str(pad.pad_number).strip(): pad
        for pad in source_package.pads
        if str(pad.pad_number).strip()
    }
    if len(source_by_name) < 2:
        return False, "insufficient_source_pads"
    if len(external_pads) < 2:
        return False, "insufficient_external_pads"

    source_ordered = sorted(source_by_name.items(), key=lambda item: _pin_sort_key(item[0]))
    external_ordered = sorted(external_pads.items(), key=lambda item: _pin_sort_key(item[0]))
    if len(source_ordered) != len(external_ordered):
        return False, "pad_count_mismatch"

    drill_tol_mm = 0.30
    size_tol_mm = 0.90
    pitch_tol_mm = 0.45

    source_drills: list[float] = []
    external_drills: list[float] = []
    for idx in range(len(source_ordered)):
        src_name, src_pad = source_ordered[idx]
        _, ext_pad = external_ordered[idx]

        src_drill = float(src_pad.drill_mm) if src_pad.drill_mm is not None else 0.0
        ext_drill = float(ext_pad.drill_mm) if ext_pad.drill_mm is not None else 0.0
        if (src_drill > 0.0) != (ext_drill > 0.0):
            return False, f"pad_plating_mismatch:{src_name}"
        if src_drill > 0.0 and ext_drill > 0.0:
            source_drills.append(src_drill)
            external_drills.append(ext_drill)

        if not _pad_size_close(src_pad.width_mm, src_pad.height_mm, ext_pad.width_mm, ext_pad.height_mm, size_tol_mm):
            return False, f"pad_size_mismatch:{src_name}"

    if source_drills and external_drills:
        src_avg = sum(source_drills) / len(source_drills)
        ext_avg = sum(external_drills) / len(external_drills)
        if abs(src_avg - ext_avg) > drill_tol_mm:
            return False, "pad_drill_mismatch"

    src_pitches = _ordered_pad_pitches([(pad.at.x_mm, pad.at.y_mm) for _, pad in source_ordered])
    ext_pitches = _ordered_pad_pitches([(pad.x_mm, pad.y_mm) for _, pad in external_ordered])
    if len(src_pitches) != len(ext_pitches):
        return False, "pad_pitch_mismatch"
    for src_pitch, ext_pitch in zip(src_pitches, ext_pitches):
        if abs(src_pitch - ext_pitch) > pitch_tol_mm:
            return False, "pad_pitch_mismatch"

    return True, None


def _ordered_pad_pitches(points: list[tuple[float, float]]) -> list[float]:
    if len(points) < 2:
        return []
    pitches: list[float] = []
    for idx in range(len(points) - 1):
        left = points[idx]
        right = points[idx + 1]
        pitches.append(math.hypot(float(left[0]) - float(right[0]), float(left[1]) - float(right[1])))
    return pitches


def _pin_sort_key(pin: str) -> tuple[int, int, str]:
    text = str(pin or "").strip()
    if text.isdigit():
        return (0, int(text), "")
    match = re.match(r"^([A-Za-z]+)(\d+)$", text)
    if match:
        return (1, int(match.group(2)), match.group(1))
    return (2, 0, text)


def _rotation_from_attr(value: str) -> float:
    text = str(value or "").strip().upper()
    if not text:
        return 0.0
    if text.startswith("M"):
        text = text[1:]
    if text.startswith("R"):
        text = text[1:]
    try:
        return float(text or "0")
    except Exception:
        return 0.0


def _safe_float(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value or "0"))
    except Exception:
        return 0.0


def _merge_equivalent_generated_parts(project: Project, ctx: MatchContext) -> None:
    if len(ctx.new_library_parts) <= 1:
        return

    canonical_by_key: dict[tuple[str, tuple, tuple], GeneratedLibraryPart] = {}
    device_aliases: dict[str, str] = {}
    symbol_aliases: dict[str, str] = {}

    for part in ctx.new_library_parts:
        key = (
            str(part.package.package_id),
            _symbol_fingerprint(part.symbol),
            tuple(sorted((str(pin), str(pad)) for pin, pad in part.device.pin_pad_map.items())),
        )
        canonical = canonical_by_key.get(key)
        if canonical is None:
            canonical_by_key[key] = part
            continue
        device_aliases[part.device.device_id] = canonical.device.device_id
        symbol_aliases[part.symbol.symbol_id] = canonical.symbol.symbol_id

    if not device_aliases and not symbol_aliases:
        return

    for component in project.components:
        if component.device_id and component.device_id.startswith("easyeda_generated:"):
            raw_device = component.device_id.split(":", 1)[1]
            mapped = device_aliases.get(raw_device)
            if mapped:
                component.device_id = f"easyeda_generated:{mapped}"
        if component.symbol_id:
            mapped_symbol = symbol_aliases.get(component.symbol_id)
            if mapped_symbol:
                component.symbol_id = mapped_symbol

    for match in project.library_matches:
        if not match.target_device or not match.target_device.startswith("easyeda_generated:"):
            continue
        raw_device = match.target_device.split(":", 1)[1]
        mapped = device_aliases.get(raw_device)
        if mapped:
            match.target_device = f"easyeda_generated:{mapped}"

    referenced_symbol_ids = {
        str(component.symbol_id)
        for component in project.components
        if component.symbol_id
    }
    referenced_device_ids = {
        str(component.device_id).split(":", 1)[1]
        for component in project.components
        if component.device_id and str(component.device_id).startswith("easyeda_generated:")
    }

    project.symbols = [symbol for symbol in project.symbols if symbol.symbol_id in referenced_symbol_ids]
    project.devices = [device for device in project.devices if device.device_id in referenced_device_ids]

    unique_parts = [
        part
        for part in canonical_by_key.values()
        if part.device.device_id in referenced_device_ids
    ]
    ctx.new_library_parts = unique_parts
    ctx.summary.created_new_parts = len(unique_parts)

    project.events.append(
        project_event(
            Severity.INFO,
            "GENERATED_LIBRARY_PARTS_DEDUPED",
            "Merged generated parts that shared equivalent symbol and footprint definitions",
            {
                "merged_device_count": len(device_aliases),
            },
        )
    )


def _symbol_fingerprint(symbol) -> tuple:
    pins = sorted(
        (
            str(pin.pin_number or "").strip(),
            str(pin.pin_name or "").strip(),
            round(float(pin.at.x_mm), 4),
            round(float(pin.at.y_mm), 4),
        )
        for pin in symbol.pins
    )
    graphics = sorted(
        tuple(sorted((str(k), str(v)) for k, v in item.items()))
        for item in symbol.graphics
    )
    return (tuple(pins), tuple(graphics))


def _component_pin_net_hints(project: Project) -> dict[str, dict[str, set[str]]]:
    hints: dict[str, dict[str, set[str]]] = {}
    for net in project.nets:
        net_name = str(net.name or "").strip()
        if not net_name:
            continue
        for node in net.nodes:
            ref = str(node.refdes or "").strip()
            pin = str(node.pin or "").strip()
            if not ref or not pin:
                continue
            hints.setdefault(ref, {}).setdefault(pin, set()).add(net_name)

    board_hints = _board_pin_net_hints(project)
    for refdes, pin_map in board_hints.items():
        for pin, names in pin_map.items():
            hints.setdefault(refdes, {}).setdefault(pin, set()).update(names)
    return hints


def _board_pin_net_hints(project: Project) -> dict[str, dict[str, set[str]]]:
    if project.board is None:
        return {}

    package_lookup: dict[str, Package] = {}
    for package in project.packages:
        package_lookup[package.package_id] = package
        package_lookup[package.name] = package

    board = project.board
    net_alias = project_track_net_aliases(project)
    board_pads = [pad for pad in board.pads if str(pad.net or "").strip()]
    board_vias = [via for via in board.vias if str(via.net or "").strip()]
    board_tracks = [track for track in board.tracks if str(track.net or "").strip()]
    if not board_pads and not board_vias and not board_tracks:
        return {}

    hints: dict[str, dict[str, set[str]]] = {}
    for component in project.components:
        refdes = str(component.refdes or "").strip()
        package_id = str(component.package_id or "").strip()
        if not refdes or not package_id:
            continue
        package = package_lookup.get(package_id)
        if package is None:
            continue

        for pad in package.pads:
            pad_number = str(pad.pad_number or "").strip()
            if not pad_number:
                continue
            world = _component_pad_world_point(component, pad.at.x_mm, pad.at.y_mm)
            net_name = _closest_board_net_name(
                world_x=world[0],
                world_y=world[1],
                board_pads=board_pads,
                board_vias=board_vias,
                board_tracks=board_tracks,
                net_alias=net_alias,
            )
            if not net_name:
                continue
            hints.setdefault(refdes, {}).setdefault(pad_number, set()).add(net_name)
    return hints


def _component_pad_world_point(component: Component, pad_x_mm: float, pad_y_mm: float) -> tuple[float, float]:
    px = float(pad_x_mm)
    py = float(pad_y_mm)
    if component.side == Side.BOTTOM:
        px = -px
    angle = math.radians(float(component.rotation_deg or 0.0))
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)
    rx = px * cos_a - py * sin_a
    ry = px * sin_a + py * cos_a
    return (float(component.at.x_mm) + rx, float(component.at.y_mm) + ry)


def _closest_board_net_name(
    world_x: float,
    world_y: float,
    board_pads,
    board_vias,
    board_tracks,
    net_alias: dict[str, str],
) -> str:
    pad_tol = 0.35
    via_tol = 0.35
    track_tol = 0.22

    best_pad: tuple[float, str] | None = None
    for pad in board_pads:
        name = _canonical_board_net_name(pad.net, net_alias)
        if not name:
            continue
        dist = math.hypot(world_x - float(pad.at.x_mm), world_y - float(pad.at.y_mm))
        if dist <= pad_tol and (best_pad is None or dist < best_pad[0]):
            best_pad = (dist, name)
    if best_pad is not None:
        return best_pad[1]

    best_via: tuple[float, str] | None = None
    for via in board_vias:
        name = _canonical_board_net_name(via.net, net_alias)
        if not name:
            continue
        dist = math.hypot(world_x - float(via.at.x_mm), world_y - float(via.at.y_mm))
        if dist <= via_tol and (best_via is None or dist < best_via[0]):
            best_via = (dist, name)
    if best_via is not None:
        return best_via[1]

    best_track: tuple[float, str] | None = None
    for track in board_tracks:
        name = _canonical_board_net_name(track.net, net_alias)
        if not name:
            continue
        dist = _distance_point_to_segment(
            world_x,
            world_y,
            float(track.start.x_mm),
            float(track.start.y_mm),
            float(track.end.x_mm),
            float(track.end.y_mm),
        )
        if dist <= track_tol and (best_track is None or dist < best_track[0]):
            best_track = (dist, name)
    if best_track is not None:
        return best_track[1]

    return ""


def _distance_point_to_segment(px: float, py: float, x1: float, y1: float, x2: float, y2: float) -> float:
    dx = x2 - x1
    dy = y2 - y1
    if abs(dx) < 1e-12 and abs(dy) < 1e-12:
        return math.hypot(px - x1, py - y1)
    t = ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    proj_x = x1 + t * dx
    proj_y = y1 + t * dy
    return math.hypot(px - proj_x, py - proj_y)


def _canonical_board_net_name(name: str | None, net_alias: dict[str, str]) -> str:
    raw = str(name or "").strip()
    if not raw:
        return ""
    return net_alias.get(raw, raw)
