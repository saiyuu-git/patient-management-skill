# Privacy, De-identification and Input Boundaries

## De-identification is the user's responsibility

Patient Management does **not** de-identify data. It reads and stores what the user submits, faithfully and locally
(SQLite on the user's machine), including names, bed numbers, record numbers, phone numbers, clinician names or any
other identifiers present in the material. It never calls a model to anonymize, and never deletes, rewrites or
anonymizes user material. The dashboard may show fields needed for patient management (e.g. name, bed).

**Before submitting material to a host agent, a model service, or this project, users must de-identify it as required
by their institution's policies and applicable laws and regulations, and remain solely responsible for patient privacy,
data security and compliance. This project does not assume that responsibility.**

Users must assess whether the chosen agent/model service may process the material under those requirements.
The project does not replace institutional security, ethics review or data-governance procedures.

## Local persistence is not local-only processing

The host agent may send submitted records to its configured third-party model service even when the final database
is local. Review the provider's data-use and privacy policies before submission. The engine provides no built-in
database encryption: protect original files, databases, agent work directories, caches and backups yourself.
AI analysis, candidate diagnoses, suggestions and knowledge supplements require clinical verification; they are not
autonomous diagnoses or orders.

## Minimal disclosure to external search

External evidence search still follows minimal disclosure: search queries contain medical concepts only — never names,
bed numbers, record ids, exact dates or patient values (enforced by code; see `docs/evidence-source-policy.md`).
Validated external evidence never contains patient data.

## Local service

The dashboard service listens on `127.0.0.1` by default; a non-loopback address requires an explicit
`--allow-remote`. The browser never reads SQLite directly; it only receives the Dashboard View Model and HTML rendered
from it. Data persists in SQLite across browser, agent-session and service restarts.

The web page is available only while the service is running; restart with the same database to restore access.
Loopback addresses refer to the current device. A phone cannot access a computer's service through the phone's
127.0.0.1. Mobile layouts do not guarantee mobile-agent installation or background-service persistence.

## Public distribution

Never publish patient records, databases, work packets, runtime outputs, private test material or textbooks.
Public project files and source policy must remain separate from private patient working data.

## Images, photos and scanned documents (no OCR here)

Patient Management implements no OCR or vision.

```
user file → host agent vision/OCR → faithful transcription → Patient Management ingestion
```

- The host transcribes faithfully; it does not interpret medically at this step and is not expected to de-identify.
- Pass transcribed text via stdin with `--text-origin host_transcription` (or `ocr` for a dedicated OCR tool).
- `text_origin` is recorded per source: `native_text`, `host_transcription`, `ocr`, `manual`.
- If the host has no vision/OCR capability, convert the file to readable text first; the engine reports scanned PDFs
  and images as unsupported instead of guessing.
