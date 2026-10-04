from __future__ import annotations

import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADMIN_SOURCE_ROOTS = tuple(
    ROOT / "admin" / crate / "src"
    for crate in ("shared", "server", "web")
)
ADMIN_BASELINE_LINES = 3916


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


def source_metrics(
    roots: tuple[Path, ...] = ADMIN_SOURCE_ROOTS,
) -> list[tuple[Path, int]]:
    return [
        (path, production_lines(path))
        for path in sorted(
            path
            for root in roots
            for path in root.rglob("*.rs")
        )
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Report admin production Rust lines before test-only modules."
    )
    parser.add_argument("--expect", type=int)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    metrics = source_metrics()
    total = sum(lines for _, lines in metrics)
    delta = total - ADMIN_BASELINE_LINES
    for path, lines in metrics:
        print(f"{lines:5} {path.relative_to(ROOT)}")
    print(f"{total:5} admin production Rust lines")
    print(
        f"Admin Rust: baseline={ADMIN_BASELINE_LINES} "
        f"current={total} delta={delta:+d}"
    )
    if args.expect is not None and total != args.expect:
        raise SystemExit(
            f"admin production Rust line count {total} does not match {args.expect}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
