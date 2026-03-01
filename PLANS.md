# PLANS.md

## Purpose

This file defines how long-running or nontrivial work should be planned and executed in this repository.

Use this file as the standard for:
- multi-step feature work
- architectural changes
- bug fixes with multiple root-cause candidates
- parser / matcher / builder / emitter changes
- any task where code changes should be sequenced and validated in stages

This file is a **planning contract**, not a place for vague notes.
Plans must be concrete, testable, and updated as work progresses.

---

## When to create a plan

Create a written plan before making code changes when a task involves any of the following:

- changes across more than one module or layer
- changes to parsing, normalization, matching, or reconstruction logic
- possible data-loss risk
- ambiguity about root cause
- new format support
- new component-class support
- changes to validation or reporting
- any task that needs regression coverage

For very small, localized edits, a full plan is optional.

---

## Plan file location and naming

For each substantial task, create or update a task-specific plan file.

Recommended location:
- `plans/`

Recommended filename format:
- `plans/YYYY-MM-DD-short-task-name.md`

Examples:
- `plans/2026-02-27-fix-instance-collapsing.md`
- `plans/2026-02-27-add-resistor-array-support.md`

If a task is continuing an existing effort, update the existing plan instead of creating duplicates.

---

## Required plan structure

Each task plan must contain the following sections, in this order.

### 1) Goal
State exactly what should be true when the task is complete.

Include:
- the user-visible outcome
- the code path(s) affected
- any explicit non-goals

### 2) Known facts
List only things already known from:
- the issue description
- current code behavior
- test failures
- validation reports
- source data

Do not mix facts with guesses.

### 3) Working theory
Describe the likely root cause(s).

This is allowed to be uncertain, but it must be specific.
Prefer a short list of plausible causes over broad hand-waving.

### 4) Scope
Define what is in scope and what is out of scope.

Include:
- files or modules likely to change
- files or modules that should not change unless necessary
- whether the task is a bug fix, enhancement, or both

### 5) Milestones
Break the task into small sequential milestones.

Each milestone should be:
- independently understandable
- small enough to complete in one focused pass
- validated before moving on

Good milestone examples:
- confirm source instance IDs are preserved through normalization
- fix board placement keying to prevent duplicate collapse
- add regression test for repeated device names
- update validation report to flag missing placed instances

### 6) Validation for each milestone
Every milestone must list how success will be checked.

Use:
- unit tests
- fixture conversions
- validation reports
- direct assertions on normalized model output
- direct assertions on emitted artifacts

Avoid “looks right” as the only validation.

### 7) Risks and guardrails
List what could go wrong if the change is done incorrectly.

Examples:
- component instances collapse by device name
- nets merge incorrectly
- source geometry is altered by a coordinate transform regression
- symbol pins lose pad-number identity
- unsupported layers get dropped silently

Also list the guardrails that must be preserved.

### 8) Decision notes
Record decisions that prevent oscillation.

Examples:
- use source instance UUID, not refdes alone, as placement identity
- board net names are authoritative when repairing schematic connectivity
- unresolved parts must be reported, not guessed
- multi-part devices must stay multi-part

### 9) Progress log
Update this section as work proceeds.

Each entry should include:
- what changed
- what was validated
- what remains blocked or uncertain

### 10) Completion check
Before marking the task done, explicitly confirm:
- code changes are scoped
- tests were added/updated
- validation passes or expected warnings are documented
- no silent data loss was introduced
- the original reported issue is actually addressed

---

## Planning rules

### Keep milestones small
A milestone should usually represent one logical change at one layer, for example:
- parser fix
- normalization fix
- instance-keying fix
- library-builder fix
- schematic connectivity fix
- report/validation fix

Do not combine unrelated fixes in one milestone.

### Fix the narrowest correct layer
Prefer fixing the earliest layer where the data becomes wrong:

- parser: source data read incorrectly
- normalization: data modeled incorrectly
- matcher: identity resolution is wrong
- builder: reconstruction is wrong
- emitter: output serialization is wrong

Do not patch later layers if the real bug is earlier.

### Stop and repair on failed validation
If a milestone’s validation fails:
- stop
- fix that failure first
- do not continue stacking changes on top of a broken milestone

### Prefer explicit uncertainty
If something is inferred or ambiguous:
- mark it as inferred
- document the uncertainty
- preserve what is known
- report what cannot be resolved

Do not hide uncertainty to make the output look cleaner.

---

## Implementation checklist template

Use this checklist inside task plans where relevant.

- [ ] Reproduce the current issue
- [ ] Identify the failing layer (parser / model / matcher / builder / emitter)
- [ ] Confirm root cause with code inspection or fixture data
- [ ] Implement the smallest correct fix
- [ ] Add or update regression tests
- [ ] Run validation on the affected conversion path
- [ ] Check for new warnings or regressions
- [ ] Update reports if the fix adds a new detectable failure mode
- [ ] Document any remaining limitations

---

## Debugging priorities for this repo

When behavior is wrong, check these first:

1. **Bad identity/keying**
   - source instance IDs dropped
   - refdes normalized incorrectly
   - placement keyed by device name instead of true instance

2. **Accidental deduplication**
   - multiple parts collapse into one
   - repeated devices overwrite each other
   - multi-part devices are flattened incorrectly

3. **Lossy net handling**
   - same-net pins not merged correctly
   - labels created without true electrical connectivity
   - power/ground nets treated as ordinary text only

4. **Format leakage**
   - EasyEDA Standard/Lite assumptions applied to Pro
   - Pro layer numbering treated like Standard
   - format-specific scaling mixed into shared logic

5. **Incomplete library generation**
   - symbol pins created without stable pad mapping
   - package geometry omitted
   - resistor arrays / multi-part devices skipped

6. **Emitter-only patching**
   - output tweaked while broken normalized data remains upstream

---

## Definition of a good plan

A good plan:
- is specific
- can be executed in order
- includes validation after each meaningful step
- records decisions clearly
- makes hidden assumptions visible
- is easy to resume after interruption

A bad plan:
- says “fix converter” with no decomposition
- mixes facts and guesses
- has no validations
- invites broad rewrites
- does not say where the bug likely lives

---

## Task plan template

Copy this block into a new file under `plans/` for each substantial task.

---

# Task: <short task title>

## Goal
- 

## Known facts
- 

## Working theory
- 

## Scope
### In scope
- 

### Out of scope
- 

## Milestones

### Milestone 1: <name>
**Intent**
- 

**Likely files/modules**
- 

**Change**
- 

**Validation**
- 

**Done when**
- 

### Milestone 2: <name>
**Intent**
- 

**Likely files/modules**
- 

**Change**
- 

**Validation**
- 

**Done when**
- 

### Milestone 3: <name>
**Intent**
- 

**Likely files/modules**
- 

**Change**
- 

**Validation**
- 

**Done when**
- 

## Risks and guardrails
- 

## Decision notes
- 

## Progress log
- [ ] Not started

## Completion check
- [ ] Issue reproduced
- [ ] Root cause identified
- [ ] Fix implemented at correct layer
- [ ] Regression test added/updated
- [ ] Validation run
- [ ] No silent data loss introduced
- [ ] Remaining limitations documented

---

## Example plan pattern for this repo

Use this pattern for common converter bugs:

1. Reproduce with a fixture and identify the failing artifact:
   - library
   - schematic
   - board
   - report

2. Trace the failure backward:
   - emitted output
   - builder output
   - normalized model
   - parsed source data

3. Confirm the first layer where the data becomes wrong.

4. Fix only that layer first.

5. Add regression coverage that would fail before the fix.

6. Re-run conversion and validate:
   - instance count
   - net connectivity
   - package mapping
   - unresolved parts
   - layer mapping

7. Only then make secondary cleanup changes.

---

## Notes for agents working in this repo

When following a plan:
- do not skip milestones silently
- do not mark a milestone complete without validation
- do not drift into broad refactors unless the plan is updated to justify it
- update the progress log as work proceeds
- if the plan becomes wrong, revise the plan before continuing

The point of planning is to prevent thrashing, not to create paperwork cosplay.

---