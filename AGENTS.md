# AGENTS.md

## Purpose

This repository contains a **Python-first converter** that imports **EasyEDA Standard/Lite** and **EasyEDA Pro** schematic + PCB files and reconstructs them into a **Fusion 360 Electronics / EAGLE-compatible project**.

When working in this repo, prioritize:
- correctness over speed
- traceability over cleverness
- explicit reporting over silent fallback
- minimal, well-scoped diffs over broad rewrites

Do not invent behavior. Do not silently drop source data. Do not “simplify” by removing fidelity.

---

## Core operating rules

1. **Do not rewrite the project from scratch.**
   - Modify the existing architecture.
   - Keep changes narrowly scoped to the requested issue.
   - Preserve module boundaries unless there is a strong architectural reason not to.

2. **Find root causes, not cosmetic patches.**
   - Fix the underlying mapping, parsing, modeling, or emission logic.
   - Do not add one-off hacks for a single part unless the issue is truly data-specific and documented.

3. **Prefer deterministic behavior.**
   - The same input should produce the same output.
   - Avoid hidden randomness, unstable ordering, or ambiguous matching when not explicitly requested.

4. **Never silently discard unsupported data.**
   - If something cannot be converted, emit a warning/report entry.
   - Preserve the source object in intermediate data if possible.

5. **Do not fake electrical meaning.**
   - Do not invent pin mappings, package geometry, or net connectivity without evidence.
   - If something is inferred, mark it clearly as inferred.

---

## Project intent

This converter must:

- parse **EasyEDA Standard/Lite** and **EasyEDA Pro** using **separate parsers**
- normalize both into a shared internal model
- generate:
  - library artifacts
  - schematic reconstruction
  - board reconstruction
  - validation reports
  - unresolved-part reports
  - layer-mapping reports
- optionally generate helper `.scr` outputs where useful

The converter is **Python-first**. Any ULP or `.scr` output is a helper mechanism, not the main logic path.

---

## Architecture expectations

Preserve and extend this general structure:

- `parsers/`
  - format-specific parsing only
- `model/`
  - normalized data classes / shared internal model
- `matchers/`
  - part/library/package matching logic
- `builders/`
  - library building
  - schematic reconstruction
  - board reconstruction
- `emitters/`
  - EAGLE/Fusion-compatible outputs
  - reports
  - helper scripts
- `ui/`
  - CLI and optional file picker
- `tests/`
  - regression tests and fixture coverage

### Separation of concerns
- Parsers should not contain emitter logic.
- Emitters should not reinterpret raw source formats directly.
- Matching logic should not be embedded ad hoc inside board or schematic emitters.
- Reports should be generated from structured validation results, not string concatenation spread across the codebase.

---

## Source-format rules

### EasyEDA Standard/Lite and Pro are different
Treat **Standard/Lite** and **Pro** as distinct source families.

Do not assume:
- identical field names
- identical coordinate systems
- identical unit scales
- identical layer numbering
- identical component identifiers

Always:
- detect source format explicitly
- apply format-specific parsing
- normalize into the shared model before reconstruction

---

## Units, geometry, and coordinates

Handle units automatically.

Required behavior:
- detect source scaling rules by format
- preserve board dimensions accurately
- preserve placement coordinates
- preserve rotation
- preserve mirroring / top-bottom side
- preserve origin behavior
- handle any required axis inversion explicitly and centrally

Do not:
- hardcode assumptions that only work for one sample board
- scatter coordinate transforms across multiple modules

All coordinate transforms should be:
- documented
- testable
- isolated in a predictable conversion path

---

## Library-generation rules

### General
Library generation must preserve:
- original pad numbers / pin numbers
- package geometry
- device-to-package mapping
- symbol pin identity
- source metadata when available

### Pin labeling
When useful and supported by the current task:
- visible pin labels may be derived from resolved board net/trace names
- original pad numbers must still be preserved separately
- duplicate labels on multiple distinct pins must **not** collapse those pins

### Matching priority
Resolve parts in this order:
1. exact MPN / exact part match
2. exact or normalized device/package match
3. generic class + package fallback
4. generate a new library part
5. report as unresolved if confidence is insufficient

### Generic passives
For standard passives (resistors, capacitors, inductors, ferrites, etc.):
- package matters
- size matters
- footprint matters
- do not treat all passives as interchangeable just because the value is similar

### Multi-part devices
Explicitly support:
- resistor arrays / resistor networks
- packages with multiple repeated elements
- symbols with multiple sections where appropriate

Do not silently flatten multi-part devices into unrelated discrete parts unless the source explicitly represents them that way.

### If a part cannot be built
Do not guess unsafe geometry.
Instead:
- keep the conversion moving where possible
- emit an unresolved-parts entry
- include enough structured metadata for manual repair

---

## Schematic-generation rules

### Connectivity source of truth
For reconstruction, **board net/trace names and resolved connectivity are authoritative**, especially when:
- the original schematic is missing
- the original schematic is incomplete
- the original schematic is inconsistent with the board

### Required behavior
The schematic builder must:
- create real electrical connectivity, not just floating labels
- connect all pins that share a resolved net
- create wires/segments as needed
- insert junctions where required
- prevent “fake nets” that are only text labels without connectivity

### Power and ground
Recognize and treat common supply nets specially.

Typical names include normalized variants of:
- GND
- AGND
- DGND
- PGND
- EARTH
- CHASSIS
- 3V3 / 3.3V
- 5V
- 12V
- VCC
- VDD
- VSS
- VBAT
- VIN
- AVDD
- DVDD

For such nets:
- create proper supply/ground symbols where supported
- connect those symbols to the electrical net
- do not leave them as ordinary dangling text labels

### Disconnected parts
A part should only be disconnected in the schematic if it is truly disconnected in the source data.
If so:
- preserve it
- report it explicitly

Do not produce large numbers of orphaned parts due to reconstruction bugs.

---

## Board-generation rules

### Instance identity
Board placement must be based on **true source component instances**, not just library/device names.

Never key board placement solely by:
- device name
- generic symbol name
- package name

Each board instance must preserve:
- unique source instance ID if available
- reference designator
- package / footprint
- X/Y position
- rotation
- side
- mirror state

### Duplicate device names
Multiple components may share the same:
- library device
- package
- generic symbol
- value

These must still remain **separate board instances**.

Do not collapse multiple resistors, headers, arrays, or repeated devices into one placement just because they map to the same library entry.

### Placement fidelity
Board reconstruction must preserve:
- all placeable parts unless explicitly excluded for a documented reason
- original coordinates
- rotation/orientation
- correct footprint assignment
- top/bottom placement

If an instance is skipped, report it.

---

## Layer-mapping rules

Layer mapping must be explicit, centralized, and testable.

Support at minimum:
- top copper
- bottom copper
- internal copper layers
- top/bottom silkscreen
- top/bottom solder mask
- top/bottom paste
- board outline / dimension
- keepout / restrict
- drill / holes
- documentation / mechanical layers

Rules:
- maintain separate mappings for Standard/Lite vs Pro
- do not rely on ad hoc numeric assumptions across formats
- unknown layers must be reported
- lossy mappings must be reported

Do not silently move unsupported layers into arbitrary destinations.

---

## Validation requirements

Every nontrivial code change should preserve or improve validation.

### Always validate:
- source component count vs output instance count
- source net count vs reconstructed net count (where meaningful)
- pad-to-net mapping integrity
- library device/package resolution
- symbol pin to package pad mapping
- board outline presence
- missing or collapsed instances
- unresolved parts
- unsupported layers / lossy mappings

### Required reports
Keep or generate structured reports for:
- validation summary
- unresolved parts
- layer mapping
- warnings / lossy conversions
- inferred elements

Reports should be:
- human-readable
- machine-readable where practical

---

## Testing rules

Every bug fix should include regression coverage.

Add or update tests for:
- parser correctness
- instance preservation
- net reconstruction
- library generation
- board placement
- multi-part device handling
- layer mapping
- report generation

When fixing a specific bug:
- add at least one test that would fail before the fix
- ensure the test proves the actual bug is solved, not just that code executed

Do not merge a bug fix without a regression test unless absolutely impossible; if impossible, document why.

---

## Code quality rules

Write code that is:
- clear
- typed where practical
- modular
- well-named
- minimally invasive
- easy to reason about

Prefer:
- small helper functions
- explicit data flow
- dataclasses / structured models
- centralized normalization rules
- reusable validators

Avoid:
- sprawling one-function conversions
- hidden global state
- “magic” hardcoded part exceptions with no explanation
- scattered special cases with no tests

---

## How to approach tasks in this repo

When asked to fix or add something:

1. Read the relevant parser, model, builder, and emitter paths first.
2. Identify where the source-of-truth data is lost, transformed incorrectly, or collapsed.
3. Fix the narrowest correct layer:
   - parser if source data is read incorrectly
   - normalization if data is modeled incorrectly
   - matcher if identity resolution is wrong
   - builder if reconstruction is wrong
   - emitter if output serialization is wrong
4. Add/update tests.
5. Update validation/reporting if new failure modes are now detectable.
6. Keep the diff scoped.

### Preferred debugging mindset
Look first for:
- bad keying / wrong unique identifiers
- incorrect normalization
- accidental deduplication
- lossy merging of nets
- invalid fallback logic
- format-specific assumptions leaking into shared logic

These are common failure modes in this project.

---

## Things you must not do

- Do not rewrite the converter into a different language.
- Do not replace the Python-first design with ULP-only logic.
- Do not remove reports to make results “look cleaner.”
- Do not collapse components because they share a device name.
- Do not drop nets because they are inconvenient to reconstruct.
- Do not label something “connected” if it is only visually adjacent.
- Do not create package geometry from guesswork.
- Do not convert uncertain multi-part devices into unrelated singles without documentation.
- Do not make broad style refactors unless explicitly requested.

---

## When uncertain

If data is incomplete or ambiguous:
- preserve what is known
- mark what is inferred
- report what is unresolved
- keep the converter honest

This project values **correct explicit uncertainty** over **confident fiction**.

---

## Definition of done

A change is considered complete only when:

- the code change is scoped and understandable
- the relevant tests pass
- a regression test covers the bug/behavior
- validation/reporting still works
- no source data is silently dropped
- the output is more faithful than before
- any unavoidable limitations are reported explicitly

---

## Task planning rules

For any nontrivial task, create and follow a written execution plan before making code changes.

A task is considered nontrivial if it involves any of the following:
- changes across more than one module or architectural layer
- parser, normalization, matcher, builder, or emitter changes
- possible data-loss risk
- ambiguous root cause
- new format support
- new component-class support
- changes to validation or reporting
- any task that requires regression coverage
- any bug fix where multiple plausible root causes exist

For trivial, tightly localized edits, a full plan is optional.

### Plan location
Store task plans in:
- `plans/`

Use this filename format:
- `plans/YYYY-MM-DD-short-task-name.md`

Examples:
- `plans/2026-02-27-fix-instance-collapsing.md`
- `plans/2026-02-27-add-resistor-array-support.md`

If a plan for the same task already exists, update it instead of creating a duplicate.

### Plan standard
Use the planning format defined in:
- `PLANS.md`

Treat `PLANS.md` as the planning contract for this repository.

### Required workflow
For nontrivial tasks:
1. Create or update the task plan.
2. Fill in:
   - Goal
   - Known facts
   - Working theory
   - Scope
   - Milestones
   - Validation per milestone
   - Risks and guardrails
3. Execute work one milestone at a time.
4. Validate each milestone before moving to the next.
5. Update the plan’s progress log as work proceeds.
6. Do not mark the task complete until the plan’s completion checklist is satisfied.

### Execution rules
- Do not skip writing a plan for nontrivial work.
- Do not make broad refactors before the plan exists.
- Do not continue to later milestones if the current milestone fails validation.
- If the working theory changes, update the plan before continuing.
- If the task becomes larger than expected, expand the plan instead of improvising across the codebase.

### Preferred shorthand
If a user asks for an “ExecPlan,” interpret that as:
- create or update a task plan in `plans/`
- follow the structure and rules in `PLANS.md`
- execute the task against that plan