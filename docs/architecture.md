# Architecture v0.2

Data contracts: `schemas/` (single source of truth). Model protocol: `docs/model-compatibility.md`. UI: `docs/dashboard-spec.md`.

Purpose: reduce repeated information gathering during ward rounds by bringing the patient's current condition, changes and outstanding tasks into one local view. Clinical analysis and patient-linked knowledge support clinician review; they do not replace clinical decisions.

## 0. Principles

1. **One Skill.** Ships as a single Skill, `patient-management`. Internally modular; never split into user-selected Skills.
2. **Specialty-agnostic.** No specialty is the default center. Specialty differences live only in data tables and in which external evidence a question retrieves.
3. **Mobile-first**, responsive to desktop.
4. **Deterministic shell.** Code owns workflow, Patient State schema, dashboard IA, layout, card order, navigation, colors, fonts, charts, empty/error/loading states, validation rules.
5. **Dynamic clinical content.** The model only produces patient-specific clinical content, inside schema fields.
6. **No model-generated UI.** Models never output HTML, CSS, JS, layout, navigation, component structure, styling, or renderable Markdown. All model text is rendered as escaped plain text.
7. **Cross-model floor.** Assume weaker models (DeepSeek, Qwen, generic APIs, future models). Goal: every model reaches a stable minimum quality, not equal ceilings. Prefer deterministic preprocessing, fixed schema, state machine, validation, retry, conservative fallback, structured knowledge, tests. No vendor-specific prompts or formats.
8. **Division of labor.** LLM: understanding + bounded clinical reasoning. Code: state, rules, computation, validation, rendering.
9. **Fail gracefully.** A failing model module degrades only its analysis area to `暂无可靠分析`; reliable data stays visible.
10. **Medical safety.** Keep `origin` distinct: `user_provided` / `system_computed` / `model_inferred` / `ai_suggestion`. Inference never auto-upgrades to user fact, diagnosis, or order.

## 1. Layers

```
patient-management (single Skill; SKILL.md tells the host agent how to drive the engine)
 L1 Ingest    register sources → deterministic parsing (tables, dates, flags, units)
 L2 Extract   model extraction with verbatim quotes → code verification → ID assignment
 L3 State     Patient State + state machines + lock rules
 L4 Compute   normalization, abnormal flags, objective series metrics, ordering, focus rules
 L5 Reason    9 analysis modules, one call each; optional bounded clinical knowledge output
 L6 Validate  validator → repair/retry → conservative fallback
 L7 View      dashboard view model incl. component states
 L8 Render    fixed static frontend renders the view model only
 evidence     on-demand web evidence via the host agent (L5 input), validated + cached; see §7
```

Only L2 and L5 involve a model. Everything else is deterministic code.

## 2. Execution model (v1)

The **host agent running the Skill is the model**. The engine is vendor-neutral, never calls an LLM API, and manages no API keys.

```
host agent                          engine (code)
  pm analysis prepare <pid> <module> ─▶ task packet: selected context + instructions + schema
  fill JSON
  pm analysis submit <package> <output> ─▶ validate → accept | repair | fallback
  pm dashboard <pid>    ──────▶  Dashboard View JSON
  pm serve --port 8765  ──────▶  fixed web dashboard
```

Optional future extension (not in v1): a direct-API adapter implementing `complete(prompt) -> text`. No other layer may depend on it. Native function calling / JSON mode are optional enhancements, never prerequisites.

Runtime: Python ≥ 3.11 (3.12 recommended). Core engine stdlib-first.

## 3. Pipeline

1. **Register** each input (admission record, progress note, lab report, investigation report, order, user message) as `source` (`src_*`). Raw text → private storage, not state.
2. **Parse deterministically** what rules can handle.
3. **Extract** the rest via `source_extraction`. Code verifies quote ⊂ source and values ⊂ quote, then assigns `fact_*` IDs. Extracted items keep `origin=user_provided`; `source_ref.extracted_by` records `deterministic_parser | model | manual`.
4. **Compute** (L4).
5. **Analyze** modules in dependency order (§9); each validated and failing independently.
6. **Render.**

Incremental ingestion updates facts and computed trends. Clinical re-analysis is a separate, authorized host step; ingestion and browser refresh do not automatically rerun models. Task packages record `input_hash`; the view supports a `stale` marker, but v1 does not automatically invalidate or schedule every affected analysis.

## 4. Patient State (v0.6)

See `schemas/patient-state.schema.json`.

```
header           display_name / alias, bed, sex, age, admission_date(+source), last_updated_at, persist_status
sources[]        provenance registry
facts[]          origin ∈ {user_provided, system_computed}; user_provided requires source_ref.extracted_by
labs.results[]   raw + normalized values, reference range, abnormal flag
labs.series[]    objective metrics only (fact_series_<test_id>)
investigations[] findings are citable facts
diagnoses[]      track A: user_provided, locked | track B: model_inferred candidate, pending review
tasks[]          explicit | ai_suggestion
analyses{}       per-module status, output, attempts, validator errors, external refs
```

Invariants:
- No model inference in `facts`, `labs`, `investigations`.
- IDs are assigned by code only.
- Raw notes are not in state; only verified `quote` snippets.

## 5. Evidence IDs

- Citable objects use `fact_<kind>_<slug>_<seq>`: `fact_lab_cr_001`, `fact_series_cr`, `fact_inv_001`, `fact_invf_001_02`, `fact_symptom_003`.
- Patient evidence IDs remain stable; corrections retain provenance and audit history. Unreviewed model candidates and suggestions may be replaced on an authorized analysis refresh (§9c).
- Every model claim carries `evidence_ids` (≥1). Validator checks each ID exists in the task packet.
- External medical evidence is cited separately via `external_refs` (`ext_*`), never mixed with patient `evidence_ids`.
- Packets list facts compactly, e.g. `[fact_lab_cr_001] 肌酐 156 μmol/L 2026-10-03T06:00 high`. Models cite IDs, never copy data.

## 6. Deterministic rules

### 6.1 Header

| Field | Rule |
|---|---|
| Label | `display_name ?? alias ?? patient_id`. User-provided names are shown as given; the system never re-anonymizes them. |
| Bed | Shown if present; no placeholder otherwise. |
| Sex / age | Parsed by code; unknown → `—`. |
| Admission date | `YYYY-MM-DD`, only from an admission record or explicit user statement. Else `入院：待补充`. Never inferred from earliest lab/note dates. |
| Last updated | Last state write. |
| `persist_status` | Local persistence only: `saved` 已更新 / `updating` 更新中 / `error` 更新失败. No cloud sync implied. |
| Hospital day | Not shown, not computed. |

### 6.2 Diagnoses (two tracks)

- **Track A, user-provided:** verbatim `display_text`, `locked=true`, status `active | resolved`. Changed only by explicit user edit. Never rewritten, normalized, or merged.
- **Track B, model candidates:** `diagnosis_candidates` may run whether or not user diagnoses exist. Output → `model_inferred`, `status=candidate`, `review_status=pending`, shown in a separate "待医生确认" area. Candidates refresh as data changes.
- A candidate **never** auto-promotes. User "accept" creates a new Track A record (`extracted_by=manual`, `from_candidate_id`); the candidate becomes `review_status=accepted`. "Dismiss" → `dismissed`.
- Candidates duplicating or rephrasing a locked diagnosis are dropped (validator V06).
- `internal_code` (e.g. ICD) is internal only; never displayed; never overrides text.

### 6.3 Labs

- **Normalization:** code conversion table; failure → `normalized_value=null`, point excluded from series.
- **Abnormal flag:** report flag first (`abnormal_flag_source=report`), else from reference range. The critical-threshold table is currently empty: critical flags come from reports, not inferred thresholds.
- **Series:** same `test_id`, comparable unit, numeric. `data_status`: `sufficient` (≥2 points) / `insufficient` (≤1) / `not_comparable`.
- **Objective metrics only** (no global % threshold, no clinical-significance judgment in code):

```
points sorted by collected_at
first_to_latest    = {absolute_change, percent_change, direction}
previous_to_latest = {absolute_change, percent_change, direction}
direction          = up | down | unchanged   (at reported precision)
overall_direction  = first_to_latest.direction
recent_direction   = previous_to_latest.direction
pattern            = monotonic_increasing | monotonic_decreasing | constant | non_monotonic
reversal_count     = sign changes between consecutive non-zero steps
reference_status_transition = {first, previous, latest} abnormal flags
percent_change     = null when base = 0
```

- **Clinical significance** = model judgment (`lab_interpretation.change_significance`) grounded in patient context + validated external evidence. Future analyte-specific deterministic criteria (e.g. guideline-defined change rules) may be added as curated deterministic rules per analyte, never as a global threshold.
- **Ordering** (key-labs order): critical > latest abnormal > ever abnormal > reference status changed > others; ties by latest collection time desc. Model never orders.

### 6.4 Tasks

```
pending → in_progress → completed
pending/in_progress → cancelled
pending → completed
```

- `explicit`: user plan, medical order, progress-note plan, ward-round plan.
- `ai_suggestion`: separate area. Accept → code creates an `explicit` task (`explicit_source=user_accepted_suggestion`, `from_suggestion_id`). Dismiss → `dismissed=true`.
- Models never change task status and never create explicit tasks.
- Dashboard checkbox actions call `set_completion`: user check → completed; user uncheck → pending (explicit reopen). Completed items sort last. This user-only path does not relax model/CLI transition rules.

### 6.5 Today's Focus

- **Rule items (code):** critical values in last 24 h; new abnormal results since last update; reference-status transitions to abnormal; new investigation reports; explicit tasks due/overdue; diagnosis changes.
- **AI items:** `clinical_assessment.focus_points` (≤3, labeled AI). On failure only rule items show.

## 7. External medical evidence (implemented: `evidence.py`)

The Skill ships **no built-in medical knowledge base**. Medical knowledge comes on demand from the web, searched by the host agent; the engine stays vendor-neutral and never calls a search API.

```
Patient State → evidence need (narrow question type + concept) → pm evidence prepare
  → dedup (same patient, same topic: pending or answered < 24h) → global cache (fresh hit = no search)
  → search package: code-built question + template queries + source policy + bundle schema
host agent searches → evidence bundle → pm evidence submit → validator → evidence_items (ext_*) + cache
```

- Search only where medical knowledge changes the analysis (lab/trend/investigation interpretation, candidate and differential diagnosis, red flags, current guideline). Never for user-stated diagnoses, objective values, trend math, task state, admission date, layout, or facts already in state.
- Queries never contain patient identifiers, record ids, dates or values. `patient_context` (age, sex, the cited facts) is for applicability only.
- External evidence is global and patient-free; patients hold only `evidence_requests` that reference `ext_*` ids. Analyses cite `evidence_ids` (patient) and `external_refs` (external) separately.
- No web / failed search → `external_evidence_status = unavailable | none_found`, UI `暂无可靠外部医学依据`; analyses needing evidence run conservatively. Never substitute pre-trained knowledge for a search.
- Source policy: `docs/evidence-source-policy.md`. Registry and templates: `knowledge/`.

## 8. Specialty coverage

No specialty content is bundled; evidence questions work for any specialty. The engine has no specialty branches. Regression cases span multiple specialties.

## 9. Modules and dependencies

| Order | Module | Input (code-selected) | Output target |
|---|---|---|---|
| 0 | source_extraction | one source text | facts / labs / investigations / dx track A / explicit tasks |
| 1 | diagnosis_candidates | fact digest + locked dx (read-only) | dx track B |
| 2 | lab_interpretation | series metrics + dx + external evidence | analyses |
| 2 | investigation_analysis | findings + prior same-type + external evidence | analyses |
| 3 | problem_list | dx + validated outputs of 2 + facts | analyses |
| 4 | clinical_assessment | problem list + key facts | analyses (incl. focus_points) |
| 4 | patient_summary | key facts + dx | analyses |
| 4 | today_focus | code-selected focus candidates | analyses (internal; not a standalone card) |
| 5 | task_suggestions | problem list + existing tasks | tasks (ai_suggestion) |
| 5 | handover_summary | all validated outputs | analyses |

Downstream modules consume only validated upstream output. If upstream fell back, downstream receives facts only.

## 9b. Natural-language ingestion (implemented)

```
pm prepare ingest <pid> <file|->  register source (fingerprint) → chunk → deterministic parse (written now)
                                  → task packages for chunks with uncovered lines
host model                        reads package.instructions, returns JSON
pm submit <package> <output>      parse → normalize → per-item schema filter → anchored verify + write
                                  → accepted | partial | repair_needed (1 retry) | failed (fallback)
```

- **Chunking** (`textparse.chunk`): split at document/section headings; pack whole lines up to `max_chars`; never split runs of lab lines, numbered lists, or markdown tables; an overlong line splits at sentence ends. Chunks keep `chunk_id`, line range, char range, `doc_kind`.
- **Parser first**: known-analyte lab lines (with an explicit collection time on the line, a lab-block header, or `--date`), `姓名/性别/年龄/床号` fields, explicit admission-date statements, numbered/semicolon diagnosis lists. Ambiguous lines are left to the model; parser-captured lab lines are listed in the package and model duplicates are skipped (V19).
- **Anchoring**: quote must be inside the task's chunks; text, values, units, reference, flag must be inside the quote; times must appear in the chunk. Stored items carry `source_ref.span`.
- **Assertion**: reliable source wording determines `present | absent | uncertain` (`textparse.assertion_evidence`). Any conflicting model assertion is rejected (V18), including a downgrade of an affirmative statement. Unclear scope is stored as `unknown`; hedged diagnoses remain `impression` facts (V17), not locked diagnoses.
- **Admission date**: explicit statement (入院日期/入院时间/`<date>入院`) or a quote from an admission record. Note/lab dates never.

## 9c. Clinical analysis engine (implemented: `analysis.py`)

```
pm analysis plan <pid>             module order + evidence needs (problem relevance and dynamic change first, max 3)
pm analysis evidence <pid>         prepare those evidence requests (cache first) -> host searches -> pm evidence submit
pm analysis prepare <pid> <module> minimal deterministic context -> task package
pm analysis submit <pkg> <out>     parse -> per-item schema filter -> clinical validator -> analyses record
pm analysis fallback <pid> <mod>   conservative fallback when the host cannot run a module
```

- Modules: patient_summary, diagnosis_candidates, lab_interpretation, investigation_analysis, problem_list, clinical_assessment, today_focus, task_suggestions, handover_summary. One module per call; each fails alone.
- Context is selected by code per module (e.g. lab: abnormal series ≤8 + returned-to-normal ≤4, related recent facts, diagnoses, linked external evidence). Downstream modules read only validated upstream output, labelled as AI analysis.
- Index date = latest dated patient datum (records may be historical), used for Today Focus recency.
- Problem order is code-tiered (worsening → linked to user diagnosis → other/candidates → resolved); the model orders only within a tier.
- Today Focus: code generates candidates (active problems, same-day events, new investigations, ≤2 abnormal labs, open explicit tasks dated near the index date); the model picks ≤3 and phrases them.
- Side effects: candidates replace earlier unreviewed candidates; AI suggestions replace earlier unreviewed suggestions. Nothing becomes a user diagnosis or explicit task without user action.
- Fallback: patient_summary / today_focus / handover_summary use deterministic templates built from existing facts; other modules store no output (UI: `暂无可靠分析`).

## 9d. Local web dashboard (implemented: `server.py`, `render.py`, `static/`)

`pm serve` (127.0.0.1 only by default) serves `/patient/<patient_id>` — one stable page per patient — rendered
server-side from the Dashboard View Model only, with vanilla JS for layout, SVG trend charts and SSE live updates.
Privacy, de-identification responsibility and the no-OCR boundary: `docs/privacy-and-input.md`.
The fixed presentation stacks overview (diagnosis/assessment/handover tabs), examinations (lab/chart + existing interpretation, or investigation facts + existing analysis), then tasks on all screens. Today Focus and Problem List remain internal; saved problem priority orders examination rows. Audit-backed additions/updates are presentation metadata only; absent history means no change claims. No duplicate header statistic tiles. Presentation updates never rerun extraction or reasoning.
The fourth presentation section, Knowledge Supplement, displays separately submitted host-generated clinical insights for physicians, grounded in patient facts and fresh verified medical evidence. `pm knowledge prepare/submit` is its CLI boundary; no bundled knowledge or render-time reasoning. Empty output is valid. Mobile/desktop share side navigation (no bottom bar), with mode controls at its bottom.

## 10. Storage and conflicts (implemented in `src/patient_management/`)

- SQLite (stdlib `sqlite3`), one DB can hold multiple patients; patient entities are scoped by patient_id, while external evidence cache entries are global and patient-free. Entity payloads are schema-shaped JSON. Migrations use `PRAGMA user_version`; state writes are transactional.
- Raw source text in `source_texts`, never in state. `events` keeps an append-only audit of entity writes.
- Duplicate sources: SHA-256 fingerprint of (kind, whitespace-squashed text), or (kind, extraction) without text; `UNIQUE(patient_id, fingerprint)`; duplicates logged in `ingest_log`.
- Conflicts (`lab_value_mismatch`, `locked_diagnosis_change`, `field_value_mismatch`) are recorded, never auto-resolved. Conflicting lab points are excluded from series until the user resolves.
- CLI `pm` prints JSON only; it is the host agent's interface to the engine.

## 11. Source bundle (Phase 7-lite)

```
patient-management/      (shipped Skill)
├─ SKILL.md
├─ README.md
├─ scripts/pm.py          dependency-free launcher (Python >=3.11)
├─ src/patient_management/ engine + prompts/ + static/
├─ schemas/
├─ docs/
└─ knowledge/              source registry + query templates (no medical content)
```

`scripts/build_skill.py` builds a local ZIP from an explicit public allowlist. No references, patient databases,
runtime outputs, development constraints or tests are shipped. Keep this source layout intact: schemas and retrieval
configuration are currently resolved beside src, not from an installed wheel.
The public project uses Apache License 2.0 (LICENSE); distributing an archive is not evidence of clinical validation.
Mobile layouts are implemented, but Operit installation and Android service lifecycle have not been verified.
