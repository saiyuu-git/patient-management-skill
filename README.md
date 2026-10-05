# Patient Management

A single, specialty-agnostic Skill with a deterministic local engine and mobile-first dashboard. The host agent extracts and reasons through structured task packages; code validates, persists, computes and renders. No vendor API integration or API keys.

## Local setup

Python 3.11 minimum; 3.12 recommended. No runtime dependencies or pip installation required.

Unpack the source bundle into a folder named `patient-management`. Keep the complete folder together: `schemas/` and `knowledge/` are runtime resources beside `src/`. Register that folder as a local Skill using your host's supported mechanism; consult its own instructions for discovery paths. The host must support local commands/files; web access and vision are optional. Only one `SKILL.md` is exposed.

From the unpacked folder, substituting a private database path:

```sh
python3 scripts/pm.py --db /absolute/private/path/pm.sqlite init
python3 scripts/pm.py --db /absolute/private/path/pm.sqlite patient create --id pt_demo --alias Demo
python3 scripts/pm.py --db /absolute/private/path/pm.sqlite prepare ingest pt_demo /absolute/private/path/note.txt
python3 scripts/pm.py --db /absolute/private/path/pm.sqlite serve --port 8765
```

Open `http://127.0.0.1:8765/patient/pt_demo`. Keep the service in a terminal or a host-supported background process. Closing it does not delete data; restart with the same database. This version does not install an OS daemon. Loopback access is restricted to the same machine; a phone cannot reach another computer through its own `127.0.0.1`. Remote/mobile deployment is not configured here.

Do not install a wheel as the supported delivery method yet: resource paths currently assume the complete source bundle. [SKILL.md](SKILL.md) describes the host workflow and retry/fallback handling. Clinical analysis and optional knowledge generation are explicit host steps, never page-render side effects.

## Privacy and medical boundaries

Users must de-identify submitted material as required by institutional policy and applicable law before providing it to an agent/model/project, and remain responsible for privacy, security and compliance. The project does not anonymize records or assume that responsibility. It is a clinician support tool, not an autonomous diagnostic or prescribing system. Local files and SQLite are not encrypted by this engine; protect access and backups yourself.

See [privacy/input](docs/privacy-and-input.md) and [medical evidence sources](docs/evidence-source-policy.md). Searches disclose only clinical concepts. No textbooks are required or shipped. Scans/images require faithful host transcription; no built-in OCR.

## Local bundle

In the source workspace, use a suitable Python interpreter:

```sh
python3 scripts/build_skill.py /absolute/output/path/patient-management.zip
```

The ZIP builder uses explicit public files/directories and does not traverse private reference/runtime folders. Symlinks are rejected; an existing output archive is never overwritten. `MANIFEST.json` contains per-file SHA-256 hashes. Review the manifest before sharing: inclusion safety does not automatically establish content licensing or clinical accuracy. Test files, fictional fixtures and patient data are not included in this public snapshot. Automated checks do not certify real-model clinical quality.

No public repository or release is created by these commands. The project is licensed under [Apache License 2.0](LICENSE).
