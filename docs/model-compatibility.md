# Model Compatibility, Validator, Fallback v0.2

Design goal: a stable minimum workflow across models, with per-module failure containment. Automated checks with simulated outputs are not clinical certification; real-model quality and mobile-agent compatibility remain unverified. Validators reduce risk but cannot guarantee factual or clinical correctness.

## 1. Assumed failure modes

Invalid JSON or prose/fences around it; missing/extra fields; wrong or localized enum values; invented numbers or IDs; directions contradicting computed metrics; orders or doses in suggestions; rewritten user diagnoses; short context (design for 8k–32k).

## 2. Reduce task difficulty (input side)

| Technique | Rule |
|---|---|
| One module per call | Never one giant JSON |
| Simple schema | Prefer small objects and shallow nesting; exact limits come from schemas/, not a universal field count |
| Bounded lists | Every array has `maxItems` |
| Precomputed facts | Metrics, flags, ordering, dates, units are given; model interprets |
| Compact fact lines | `[fact_id] name value unit time flag`; model returns IDs |
| Selected context | Only relevant facts and ≤3 validated external sources per question, budget-truncated |
| Model-neutral packets | Module-specific instructions + selected input + schema; examples only where provided; no vendor-specific format |
| Honest abstention | `status=insufficient_information` is valid |
| Low temperature | ≤0.3 when the host allows |

No model-specific prompts. Function calling, JSON mode, long reasoning, and long context are never prerequisites.

## 3. Parsing (deterministic, before validation)

1. Strip code fences and surrounding prose; take the first complete JSON object.
2. Safe repairs: trailing commas, CJK quotes, full-width colons, BOM.
3. Lenient enum mapping (case, whitespace, common Chinese synonyms such as 可能→`possible`) via a code table.
4. Drop unknown fields (logged).

Still no object → attempt failed.

## 4. Validator rules

Levels: **R** reject module → repair/retry. **D** drop offending item, keep the rest. **W** log only.

| ID | Rule | Level |
|---|---|---|
| V01 | Parses to a JSON object | R |
| V02 | `module` matches request; top-level required fields present | R |
| V03 | Item fields: required, type, enum, length | D |
| V04 | Every `evidence_id` exists in the task packet; invalid IDs removed, item dropped if none remain | D |
| V05 | Numbers in text (decimals, numbers with units) must match the cited facts, patient state, or claims of the cited external evidence (format/unit tolerant) | D |
| V06 | Candidate duplicating or rephrasing a locked user diagnosis (normalized-text match) | D |
| V07 | Dose patterns (number + mg/g/ml/U/片 …) or order-style verbs (立即给予, 予…, 停用…) in AI text | D |
| V08 | Direction words in lab text contradict computed `overall_direction`/`recent_direction` | D |
| V09 | `certainty≠likely` problem lacks `uncertainty`; `insufficient_information` output contains `likely` claims | D |
| V10 | Contradictory claims about the same fact within a module → drop both | D |
| V11 | HTML tags, `<script`, Markdown headings/tables/links in any field → strip; drop if nothing meaningful remains | W/D |
| V12 | Item count above limit → truncate | W |
| V13 | Extraction: quote not a substring of source, or value digits not in quote | D |
| V14 | Extraction: admission date quote not from admission record or explicit user statement | D |
| V15 | Zero valid items while `status=ok` | R |
| V17 | Extraction: hedged wording (考虑/可能/待排/不除外/?) submitted as a stated diagnosis | D |
| V18 | Extraction: any model assertion conflicts with reliably determined source wording; no upgrades or downgrades | D |
| V19 | Extraction: lab already captured by the deterministic parser (skipped, not an error) | — |
| V16 | `change_significance ≠ uncertain` with `certainty=likely` but no `external_refs` → downgrade certainty to `possible` | W |

V05/V07/V08 v1 = rules + word lists, not semantic proof. For extraction, reliable source cues own assertion; unclear negation scope stays `unknown`. A "more conservative" assertion is not accepted if it changes the source meaning.

## 5. Retry state machine

```
attempt 1 → parse → validate ──R/V15──▶ attempt 2 (repair: packet + previous output + error list)
                                              └──R──▶ conservative fallback (status=fallback)
```

- Max 2 attempts. Timeouts, empty output, host interruption = R.
- D errors never trigger retry. If >50% of items are dropped, component state = `partial` with notice.

## 6. Conservative fallback

Available regardless of model modules: header facts, locked user diagnoses, objective labs/trends, investigation facts and explicit tasks. Fixed presentation rules still select visible examinations. Today Focus and Problem List remain internal, not standalone dashboard cards.

| Module | Fallback |
|---|---|
| patient_summary | Code template: sex, age and supplied diagnoses (or `待补充`). Header remains separate. No judgment. |
| diagnosis_candidates | Candidate area hidden. Track A unaffected. |
| lab_interpretation | Data + metrics unchanged; analysis area `暂无可靠分析` |
| investigation_analysis | Findings + impression unchanged; analysis `暂无可靠分析` |
| problem_list | No generated assessment; user diagnoses remain available separately |
| clinical_assessment | `暂无可靠分析`; objective data remains available |
| task_suggestions | AI suggestion area hidden |
| today_focus | Code-selected existing events/tasks; internal output, not a standalone card |
| handover_summary | Fixed fields from demographics/admission date, chief complaint, selected lab trends, supplied diagnoses and code-selected focus; no inferential assessment |
| knowledge_supplement | Empty output; no unsupported medical knowledge is invented |

Templates restate existing facts only; no inferential wording.

## 7. Module selection

The host selects modules required by the authorized workflow and invokes fallback where necessary. No `model_profile` CLI switch or automatic model-specific profile is implemented. Selection never changes schemas or UI structure.

## 8. Cross-model regression

- Fictional multi-specialty golden cases with expected facts, metrics, and a must-not-appear list.
- Metrics: parse rate, module pass rate, item drop rate, V05/V07 hits, fallback rate, render crashes.
- **Target:** no module failure breaks the page, no unsupported facts are accepted, reliable data stays available. This is an acceptance goal, not a guarantee of clinical correctness or exhaustive display.
- Adversarial fixtures (broken JSON, fake IDs, HTML injection, dose orders) test validator + frontend without any real model.
- Tests and fictional fixtures are local-only and are not included in the public repository snapshot.

## 9. External evidence bundle validator (`evidence.py`)

| ID | Rule | Effect |
|---|---|---|
| E01 | Bundle parses to an object; source is well-formed | reject bundle / source |
| E02 | `request_id` and `module` match the package | reject bundle |
| E03 | URL valid (http/https, real host, no spaces) | drop source |
| E04 | Title present | drop source |
| E05 | publication_date `YYYY[-MM[-DD]]`, accessed_at ISO | drop source |
| E06 | `source_type` in the allowed enum | drop source |
| E07 | Domain not on the disallowed list (social, forum, encyclopedia, commercial health) | drop source |
| E08 | 1–5 claims, each ≤300 chars | drop source |
| E09 | No patient data in evidence (identifiers, record ids, dates, "该患者/this patient") | drop source |
| E10 | No conclusions outside sources (`summary`, `answer` …) | field ignored |
| E11 | More than `max_sources` → keep best tier first | truncate |
| E12 | If the host returned search metadata: URL must be in it, title must match | drop source |
| E13 | Declared tier differs from code tier (tier follows source_type) | code wins |
| E14 | Duplicate URL | drop duplicate |
| E15 | No source survived | repair (1×) then failed |
| E16 | Content not accessed/verified (`snippet_only` or missing verification) | unverified_candidate only; never citable external evidence |

Source access and claim grounding are host responsibilities; code checks supplied verification metadata and references, not independent page entailment.

## 10. Analysis validator additions (`analysis.py`)

| ID | Rule | Effect |
|---|---|---|
| V04 | evidence_ids must be in the module's context; external_refs must be validated evidence provided to the module; candidates/suggestions need ≥1 `fact_*` | drop item |
| V05 | Numbers must appear in the cited evidence lines, cited external claims, or basics | drop item |
| V07 | Doses/administration wording anywhere; order wording in suggestions | drop item |
| V08 | Trend wording contradicts the computed direction | drop item |
| V20 | Hypothesis written as confirmed (确诊, 诊断明确, confirmed) | drop item |
| V21 | `assertion=unknown` facts as sole or "likely" support | drop item |
| V22 | `assertion=absent` facts cited for a claim without negation | drop item |
| V23 | Causal wording without hedging | drop item (lab: only that explanation) |
| V24 | Investigation item without its investigation/finding ids | drop item |
| V25 | Today Focus item not from a code candidate, or evidence outside it | drop item |
| V26 | AI suggestion duplicates an explicit task | drop item |

## 11. Knowledge Supplement

`schemas/knowledge-supplement.schema.json` limits output to 0–3 patient-linked insights, with title, why_relevant,
knowledge, clinical_connection, patient evidence_ids, verified external_refs and uncertainty. Facts describe this
patient; medical sources describe general knowledge. Neither may substitute for the other.

Code checks schema, known patient references, linked fresh page-verified sources and basic unsafe-text patterns.
The host must check relevance, claim grounding, duplication, uncertainty and unsupported causality before submission;
ID validation alone does not establish clinical correctness. One repair, then an empty result on failure.
