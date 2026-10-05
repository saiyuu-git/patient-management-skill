"""Accept bounded host education, never generate it during rendering."""
import re
from pathlib import Path

from . import evidence, provenance, schema, state
from .persistence import PMError, now


def eligible(db, patient_id):
    known = provenance.known_fact_ids(db, patient_id) | {d['id'] for d in db.all('diagnoses', patient_id)}
    fresh = {eid for c in db.all_global('evidence_cache') if evidence.cache_status(c) == 'fresh' for eid in c['evidence_ids']}
    links = {eid: set(r['relevant_fact_ids']) for r in db.all('evidence_requests', patient_id)
             if r['status'] in ('answered', 'cached') for eid in r['evidence_ids'] if eid in fresh}
    return known, {x['evidence_id']: links[x['evidence_id']] for x in evidence.items(db, list(links))}


def submit(db, patient_id, output):
    errors = schema.validate(output, 'knowledge-supplement.schema.json')
    if errors:
        raise PMError(errors[0])
    known, links = eligible(db, patient_id)
    for item in output['knowledge_items']:
        ids = set(item['evidence_ids'])
        if not ids <= known or any(ref not in links or not ids & links[ref] for ref in item['external_refs']):
            raise PMError('unknown, stale, unverified or unrelated evidence')
        text = ' '.join(v for v in item.values() if isinstance(v, str))
        if re.search(r'<[^>]*>|\[[^\]]+\]\(https?://|\d+(?:\.\d+)?\s*(?:mg|ml|μg|片)|立即给予|确诊为', text, re.I):
            raise PMError('unsupported markup, prescription or diagnosis wording')
    # ponytail: semantic grounding is host-reviewed; IDs cannot prove paraphrase entailment.
    state.put_analysis(db, patient_id, 'knowledge_supplement', {
        'status': 'ready' if output['knowledge_items'] else 'fallback', 'attempts': 1, 'output': output, 'generated_at': now()})
    return {'status': 'accepted', 'count': len(output['knowledge_items'])}


def prepare(db, patient_id):
    """Reuse existing selected analysis context; no new search or inference."""
    from . import analysis
    context = analysis.build_context(db, patient_id, 'clinical_assessment')
    _, links = eligible(db, patient_id)
    sources = evidence.items(db, [eid for eid in context['ext'] if eid in links])[:3]
    return {'patient_id': patient_id, 'package_type': 'pm.knowledge_task',
            'patient_context': context['lines'], 'external_evidence': sources,
            'output_schema': schema.inline('knowledge-supplement.schema.json', ''),
            'instructions': Path(__file__).with_name('prompts').joinpath('knowledge-supplement.txt').read_text(),
            'submit_with': 'pm knowledge submit <patient_id> <output file>'}
