"""Build a local source bundle from public files only; never traverse private folders."""
import argparse
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]
FIXED = ('SKILL.md', 'README.md', 'pyproject.toml', 'scripts/pm.py',
         'knowledge/source-registry.json', 'knowledge/query-templates.json', 'knowledge/README.md')
TREES = {'docs': {'.md'}, 'schemas': {'.json'}, 'src/patient_management': {'.py'}}
ASSETS = {'src/patient_management/prompts': {'.txt'}, 'src/patient_management/static': {'.css', '.js', '.png', '.svg'}}


def public_files(root=ROOT):
    files = [root / name for name in FIXED]
    for directory, suffixes in {**TREES, **ASSETS}.items():
        folder = root / directory
        if folder.is_symlink():
            raise ValueError(f'symlink directory refused: {directory}')
        files.extend(p for p in folder.iterdir() if p.suffix in suffixes and not p.name.startswith('.'))
    for p in files:
        if p.is_symlink() or not p.is_file() or not p.resolve().is_relative_to(root.resolve()):
            raise ValueError(f'non-public file refused: {p.name}')
    return sorted(set(files))


def build(output, root=ROOT):
    paths = public_files(root)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    # Exclusive creation: never overwrite an existing delivery archive.
    with ZipFile(output, 'x', compression=ZIP_DEFLATED) as archive:
        for p in paths:
            archive.write(p, 'patient-management/' + p.relative_to(root).as_posix())
        archive.writestr('patient-management/MANIFEST.json', json.dumps(manifest, sort_keys=True, indent=2))
    return {'archive': str(output), 'files': len(paths), 'bytes': output.stat().st_size}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('output', help='new ZIP path; existing paths are refused')
    print(json.dumps(build(parser.parse_args().output)))
