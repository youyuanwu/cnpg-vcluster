from __future__ import annotations

import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTROLLER_SRC = ROOT / "controller" / "src"


def production_lines(path: Path) -> int:
    lines = path.read_text(encoding="utf-8").splitlines()
    return next(
        (
            index
            for index, line in enumerate(lines)
            if line.strip() == "#[cfg(test)]"
        ),
        len(lines),
    )


def source_metrics(source: Path = CONTROLLER_SRC) -> list[tuple[Path, int]]:
    return [
        (path, production_lines(path))
        for path in sorted(source.rglob("*.rs"))
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Report Rust production lines before test-only modules."
    )
    parser.add_argument("--expect", type=int)
    parser.add_argument("--max", dest="maximum", type=int)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    metrics = source_metrics()
    total = sum(lines for _, lines in metrics)
    for path, lines in metrics:
        print(f"{lines:5} {path.relative_to(ROOT)}")
    print(f"{total:5} production Rust lines")
    if args.expect is not None and total != args.expect:
        raise SystemExit(
            f"production Rust line count {total} does not match {args.expect}"
        )
    if args.maximum is not None and total > args.maximum:
        raise SystemExit(
            f"production Rust line count {total} exceeds {args.maximum}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
