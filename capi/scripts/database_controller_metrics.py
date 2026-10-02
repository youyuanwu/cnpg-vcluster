from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.controller_metrics import production_lines

MAX_PRODUCTION_LINES = 12_000


def main() -> int:
    source = ROOT / "database-controller" / "src"
    metrics = [
        (path, production_lines(path))
        for path in sorted(source.rglob("*.rs"))
    ]
    total = sum(lines for _, lines in metrics)
    for path, lines in metrics:
        print(f"{lines:5} {path.relative_to(ROOT)}")
    print(f"{total:5} database-controller production Rust lines (max {MAX_PRODUCTION_LINES})")
    if total > MAX_PRODUCTION_LINES:
        raise SystemExit(
            f"database-controller production Rust line count {total} "
            f"exceeds {MAX_PRODUCTION_LINES}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
