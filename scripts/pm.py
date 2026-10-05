"""Run the unpacked Skill without pip or a dependency install."""
import json
import sys
from pathlib import Path

if sys.version_info < (3, 11):
    print(json.dumps({'error': 'Python >=3.11 required; 3.12 recommended'}), file=sys.stderr)
    raise SystemExit(1)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from patient_management.cli import main

if __name__ == '__main__':
    raise SystemExit(main())
