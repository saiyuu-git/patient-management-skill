# Evidence Source Policy v0.1

External medical evidence supports, but never replaces, patient facts. It is retrieved on demand by the host agent, validated by code, and cached locally.

This supports patient-linked analysis and knowledge supplements, not a separate medical-search product or a bundled textbook library. Search capability depends on the host.

## Source tiers

| Tier | Source types (`source_type`) | Examples |
|---|---|---|
| 1 | `official_guideline`, `government_health_agency`, `professional_society_statement`, `consensus_statement` | WHO, CDC, NICE, national health commissions, AHA/ASA, ACC, ESC, KDIGO, IDSA, ADA, ACOG, AAP, national specialty societies |
| 2 | `systematic_review`, `peer_reviewed_review` | Cochrane reviews; PubMed-indexed guideline reviews and authoritative journal reviews |
| 3 | `original_research` | Peer-reviewed original studies |
| 4 | `institutional_education` | Patient/clinician education pages of reliable medical institutions |

- The tier is assigned by code from `source_type`; a tier declared by the model is advisory.
- `knowledge/source-registry.json` lists well-known bodies as **hints** (`tier_basis = registry`). Unknown but reliable professional bodies are allowed (`tier_basis = declared`); the policy is open across all specialties.
- Prefer Tier 1, then Tier 2. Default `max_sources = 3`; when more are returned, the best tiers are kept.

## Never acceptable as medical evidence

Forums, Q&A sites, social media, video platforms, encyclopedias, commercial health websites and SEO content (see `disallowed_domains`). Pre-trained model knowledge presented as if retrieved.

Search snippets are for source discovery only. An inaccessible or unverified page is an `unverified_candidate`, not an `ext_*` claim; it cannot enter `external_refs` or support clinical conclusions. The host must access and check the relevant content before declaring `verification=page_accessed`. Code validates this declaration and metadata; it does not independently prove that the claim is supported by the page.

## What a source must contain

URL, title, source type, access date; publication date when shown; 1–5 short claims, each one point as the source states it (hedging kept); applicability and limitations. No patient data and no conclusions outside a source.

## When to search

Only when medical knowledge changes the analysis: interpreting an abnormal lab, the meaning of a trend, an investigation finding, candidate or differential diagnosis, red flags, current guideline/standard, or a question deterministic rules cannot answer. Never for user-stated diagnoses, objective values, trend arithmetic, task status, admission date, layout, or facts already in patient state.

## Freshness

- Cache states: `fresh`, `stale` (past TTL), `invalidated` (manual). Only fresh entries are reused.
- TTL: 180 days for guideline/standard questions, 365 days otherwise.
- Questions asking for the latest/current guideline, consensus or standard always search again (`recency = latest`).

## When search is impossible

`external_evidence_status = unavailable` (no web) or `none_found` (nothing reliable). The UI shows `暂无可靠外部医学依据`; patient data, user diagnoses, objective trends, investigation facts and explicit tasks remain fully available.
