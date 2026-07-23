"""Delete pipeline run artifacts under data/sessions (keyframes are transient).

Usage:
    .venv/bin/python harness/clean_sessions.py --dry-run
    .venv/bin/python harness/clean_sessions.py --keep 1     # keep newest run per session
    .venv/bin/python harness/clean_sessions.py --all
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_BASE = Path(__file__).resolve().parent.parent / "data" / "sessions"


def run_dirs(base: Path) -> dict[Path, list[Path]]:
    sessions: dict[Path, list[Path]] = {}
    if not base.is_dir():
        return sessions
    for session_dir in sorted(base.glob("ses_*")):
        runs = sorted(d for d in session_dir.glob("run_*") if d.is_dir())
        if runs:
            sessions[session_dir] = runs
    return sessions


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", type=Path, default=DEFAULT_BASE)
    ap.add_argument("--keep", type=int, default=0, help="newest runs to keep per session")
    ap.add_argument("--all", action="store_true", help="required to delete with --keep 0")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.keep == 0 and not args.all and not args.dry_run:
        ap.error("refusing to delete everything without --all (or use --dry-run)")

    doomed: list[Path] = []
    for _session_dir, runs in run_dirs(args.base).items():
        keep = args.keep if args.keep > 0 else 0
        doomed.extend(runs[: len(runs) - keep] if keep else runs)

    for path in doomed:
        print(("DRY-RUN would delete: " if args.dry_run else "deleting: ") + str(path))
        if not args.dry_run:
            shutil.rmtree(path)
    print(f"{len(doomed)} run dir(s) {'listed' if args.dry_run else 'deleted'}")


if __name__ == "__main__":
    main()
