# Model Compatibility, Validator, Fallback v0.2

Goal: every model reaches a stable minimum quality. Stronger models write better analysis; weaker models never break the page or fabricate facts.

## 1. Assumed failure modes

Invalid JSON or prose/fences around it; missing/extra fields; wrong or localized enum values; invented numbers or IDs; directions contradicting computed metrics; orders or doses in suggestions; rewritten user diagnoses; short context (design for 8k–32k).

## 2. Reduce task difficulty (input side)

| Technique | Rule |
|---|---|
| One module per call | Never one giant JSON |
| Flat schema | Nesting ≤2, ≤8 fields per object, ≤8 enum values |
| Bounded lists | Every array has `maxItems` |
| Precomputed facts | Metrics, flags, ordering, dates, units are given; model interprets |
| Compact fact lines | `[fact_id] name value unit time flag`; model returns IDs |
| Selected context | Only relevant facts and ≤3 validated external sources per question, budget-truncated |
| One packet template | Instructions + rule list + input + schema + 1 minimal example; identical for all models |
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
| V18 | Extraction: `assertion=present` while the quote carries a negation/uncertainty cue | D |
| V19 | Extraction: lab already captured by the deterministic parser (skipped, not an error) | — |
| V16 | `change_significance ≠ uncertain` with `certainty=likely` but no `external_refs` → downgrade certainty to `possible` | W |

V05/V07/V08 v1 = rules + word lists; prefer false rejects over false accepts.

## 5. Retry state machine

```
attempt 1 → parse → validate ──R/V15──▶ attempt 2 (repair: packet + previous output + error list)
                                              └──R──▶ conservative fallback (status=fallback)
```

- Max 2 attempts. Timeouts, empty output, host interruption = R.
- D errors never trigger retry. If >50% of items are dropped, component state = `partial` with notice.

## 6. Conservative fallback

Always shown regardless of model modules: header facts; locked user diagnoses; real lab values, flags, objective series metrics and charts; investigation findings and verbatim impressions; explicit tasks; rule-based focus items.

| Module | Fallback |
|---|---|
| patient_summary | Code template: label, sex, age, user diagnoses (or `诊断：待补充`). No judgment. |
| diagnosis_candidates | Candidate area hidden. Track A unaffected. |
| lab_interpretation | Data + metrics unchanged; analysis area `暂无可靠分析` |
| investigation_analysis | Findings + impression unchanged; analysis `暂无可靠分析` |
| problem_list | If user diagnoses exist, titles from verbatim diagnoses without assessment; else `暂无可靠分析` |
| clinical_assessment | `暂无可靠分析`; focus shows rule items only |
| task_suggestions | AI suggestion area hidden |
| handover_summary | Code SBAR template from facts only (header, user dx, abnormal labs with metrics, new impressions, open explicit tasks); analysis lines `暂无可靠分析` |

Templates restate existing facts only; no inferential wording.

## 7. Optional run profile

`model_profile` selects modules only; schemas and UI unchanged. `standard` = all. `basic` = extraction + lab_interpretation + investigation_analysis; others use fallback templates. Set by user/host config, never by model name.

## 8. Cross-model regression

- Fictional multi-specialty golden cases with expected facts, metrics, and a must-not-appear list.
- Metrics: parse rate, module pass rate, item drop rate, V05/V07 hits, fallback rate, render crashes.
- **Floor:** zero crashes, zero fabricated facts displayed, 100% of reliable data displayed.
- Adversarial fixtures (broken JSON, fake IDs, HTML injection, dose orders) test validator + frontend without any real model.

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
