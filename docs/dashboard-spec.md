# Dashboard Spec v0.3

Knowledge Supplement targets physicians: at most three high-value patient-linked clinical insights, not disease introductions or another overview. Host output is `knowledge_items` with title, why_relevant, knowledge, clinical_connection, patient evidence_ids, verified external_refs, and uncertainty (low/moderate/high). Fixed labels: `为何值得关注 / 临床要点 / 联系本例 / 不确定性`. Empty output is valid. Preserve patient facts vs general medical knowledge; never convert associations into patient-specific causes.

Sidebar icons share one blue filter and centered 24px slots; text is vertically centered in the same slots. The overview main heading has no icon. Source links use `来源` without tier badges (tiers remain internal validation metadata). Chart points show a compact rounded-border tooltip following the pointer; pointer exit hides it. Touch clicks show it temporarily (2.5s), outside click/scroll/Escape dismiss it. Keyboard Enter also displays it; blur dismisses it. Keyboard focus uses a circular stroke, not a rectangular enlargement.

Host education is accepted by `education.submit`, persisted in `analysis_records` as `knowledge_supplement`, and projected through the fixed view. Each entry links patient evidence and fresh, page-verified medical sources. Invalidated/stale/unrelated sources hide the entry. Semantic paraphrase grounding remains host-reviewed, not proven by ID validation. No render-time search/model calls. The overview icon uses one fixed SVG silhouette (circle head and rounded shoulders), not separately positioned CSS pieces.

Renderer input is only the Dashboard View Model. Presentation changes never trigger extraction, web search or reasoning.

## Fixed presentation

1. Patient header: supplied name/alias/id, bed, sex, age, admission date, last update and local save status.
2. `患者概览`: tabs `诊断 / 病情分析 / 汇报` in that fixed order.
3. `辅助检查`: `检验 / 检查` switch, complete vertical project list, then selected project detail.
4. `今日待办`: explicit tasks and a separate suggestion area.
5. `知识补充`: high-value clinical knowledge for physicians about this patient's active problems, mechanisms, diagnostic traps, discordance, complications or monitoring. No basic disease introductions or repeated overview. Prompt: `src/patient_management/prompts/knowledge-supplement.txt`; contract: `schemas/knowledge-supplement.schema.json`. Saved host output is rendered through the fixed view; legacy verified claims remain available only when no knowledge record exists. Missing evidence → empty, never invented knowledge.

No separate Patient Summary detail fields, Today Focus or Problem List card. Stored outputs remain available internally; no data is deleted. Existing handover content is reused, including its focus field when present. The view's `presentation` defines grouping; legacy module payloads remain unchanged.

## Diagnosis

Preserve locked diagnoses verbatim and in order. Show all diagnoses without individual folding: desktop two columns in row-major order, mobile one column. Candidates remain separate under `候选诊断 · 待医生确认`. No ICD codes.

### Overview change marks

Use patient-scoped existing audit events, never a model comparison. Diagnoses compare to the state known by the previous calendar day (local record time); first import without a prior baseline is not all-new. Backdated sources are not marked new diagnoses. New records show `新`; edits of an existing diagnosis show `更新`. These badges mean record/content additions, not new disease onset.

Assessment and handover compare with the immediate prior saved ready/partial analysis version (exclude pending/error events). Matching text is unchanged; changed wording on overlapping evidence is `更新`, otherwise an added statement is `新`. This is a textual/evidence diff, not a new clinical judgment. Show the baseline date/basis. Missing history → `暂无此前记录可供比较`, no invented marks. Existing SQLite audit preserves future versions without a new database or analysis run.

## Examination list/detail

- Fixed `检验 / 检查` switch beside the title; no horizontally scrolling project tabs.
- Vertical rows show name plus latest lab value/unit/trend or investigation date. Click for detail, `返回项目列表` to return.
- Lab detail: values and objective chart, followed immediately by existing clinical interpretation. Investigation detail: findings/impression followed by existing analysis. No separate interpretation card or invented results/images.
- Visible projects are unfolded. Hide blood grouping, platelet antibody and irregular antibody screens by default (requested presentation exclusions, not a claim that these tests are universally useless). Also hide non-comparable, uninterpreted noncritical labs; never hide critical flags solely for missing analysis. Retain all source facts. Explicit user request: `pm dashboard <pid> --include-background-labs`, or patient/API URL `?include_background_labs=1`. SSE retains that opt-in.
- Sort visible labs with critical flags first, then existing Problem List evidence priority; use existing relation/series order for ties. Investigations use the same linkage, then relation/date order. No new urgency reasoning. Stable IDs preserve open detail on reorder.
- ≥2 comparable points: chart; one point: single value; incompatible units: raw values without a false trend. Touch/keyboard inspection gives time, value, unit; all view-supplied points stay accessible.
- Invalid external references remain hidden. Missing analysis → `暂无可靠分析`, without hiding reliable data.

## Task write boundary

Native checkboxes for explicit, non-cancelled tasks only. User check → completed; user uncheck → pending. Completed text is struck through and sorted last. AI suggestions have no completion checkbox. Hints show `可能已完成，待确认` and never auto-change state.
Each explicit task carries a blue `用户` prefix (an explicit plan extracted from supplied notes/transcribed speech, or explicitly accepted by the user). Each unaccepted suggestion carries a purple `AI` prefix and remains after user tasks. Existing provenance/extraction pipeline determines attribution, never free UI text. Remove the redundant completed badge; keep the checkbox and strike-through. Success does not leave a permanent status line below the list.

POST `/api/patient/<id>/task-completion`: schema-valid JSON (`task_id`, boolean `completed`), bounded request size, same-origin loopback Host/Origin and custom action header. Writes are transactional, audited, patient-scoped and persisted in SQLite. Failed save restores the checkbox and shows `保存失败，请重试`.

## Layout and navigation

Every screen: one vertical reading column in canonical order, ≥44px touch targets. Four main sections use native open/close controls; navigation opens its target. Both modes use the same side navigation: `患者概览 / 辅助检查 / 今日待办 / 知识补充`. Desktop can collapse to an icon rail; mobile uses a drawer. No bottom navigation: avoid browser-toolbar overlap. Overview and examination tabs are identical in both modes.

`移动版 / 桌面版` controls sit at the bottom of the side toolbar, as plain text with underline separation/selection (no boxed buttons), and override initial viewport mode. Preferences are local browser UI settings only. No separate rendering/data implementation. The overview navigation icon is a person silhouette, not a house.
The overview icon has a circular head and half-ellipse torso with no tile background. All sidebar icons share the same blue color (knowledge included). Section controls use a standard CSS chevron plus `展开 / 收起`. Clicking the top brand returns to scroll position zero, rather than the overview card.

Handover: `相关病史` is an unfolded peer of other fields. No separate `主要检查结果` subsection; append existing findings to `主要问题`, deduplicating identical text and preserving change marks. Do not generate new medical summaries.
Assessment `尚待明确的问题` is also an unfolded peer section; never a details/summary disclosure.

SSE replaces changed group fragments without page reload; preserve overview tab, examination selection and main-section open/closed state. Redraw charts when a section reopens. No reasoning is invoked. Header contains patient information only, without diagnosis/task/lab statistic tiles.

## States and copy

Underlying modules retain `ready / partial / empty / loading / error`; failures remain isolated. Reliable examination data stays available when analysis fails.

| Case | Fixed copy |
|---|---|
| No reliable analysis | 暂无可靠分析 |
| No external evidence | 暂无可靠外部医学依据 |
| Partial / stale | 仅显示已核实的分析内容 / 资料已更新，分析待更新 |
| Module error | 该模块暂时无法显示 |
| No labs / investigations | 暂无检验数据 / 暂无辅助检查 |
| No diagnosis / tasks | 诊断：待补充 / 暂无待办 |
| No education evidence | 暂无与当前患者相关的可靠知识补充 |
| Admission unknown | 入院：待补充 |
| Save status | 已更新 / 更新中 / 更新失败 |
| Model analysis | 智能辅助分析 |
| Suggestions | 智能建议 · 需医生确认 |

## Design and safety

Fixed section accents: slate/rose for overview, teal/violet for examinations, orange for tasks; soft header tints and white reading surfaces. Charcoal body, readable grey metadata. Accents identify content, not clinical severity. Amber/critical badges indicate reference abnormalities; chart direction never means good/bad.
Knowledge Supplement uses one muted violet accent for its heading, item titles, links and notes. Private preview samples are explicitly labelled illustrative, are not validated medical evidence, and never enter a real patient's state or evidence cache.

System Chinese fonts; 13–15px body, 16–17px titles, 12px metadata; rounded cards and restrained borders/shadows. Keyboard access, visible focus indicators and native controls.
Header avatar follows supplied sex: male light-blue background/blue silhouette, female light-pink background/pink silhouette, unknown neutral grey. Color never infers or overrides sex.

Escape every dynamic value. Localize known technical enums in analysis without changing meaning. Preserve locked diagnoses, source findings, medical abbreviations and units. No raw Patient State/source documents, executable patient text or model-generated UI.

Privacy and host transcription boundary: `privacy-and-input.md`.
