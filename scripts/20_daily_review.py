from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qbg.analysis.health import diagnose  # noqa: E402

if __name__ == "__main__":
    print(json.dumps([finding.as_dict() for finding in diagnose()], ensure_ascii=False, indent=2))

