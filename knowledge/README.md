# knowledge/ — evidence retrieval configuration (no medical content)

The Skill bundles no medical knowledge base. Medical evidence is searched on demand by the host agent
(see `docs/architecture.md` §7 and `docs/evidence-source-policy.md`). This folder only holds configuration:

- `source-registry.json` — authority hints (organizations → domains) and disallowed domains. Hints, not a whitelist.
- `query-templates.json` — fixed query templates per question type and language; code fills in the concept.

Evidence request / bundle / item / cache schemas: `schemas/evidence.schema.json`. Engine: `src/patient_management/evidence.py`.
