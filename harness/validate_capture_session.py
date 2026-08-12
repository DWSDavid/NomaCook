"""Validate a capture session directory: schema, monotonicity, landmarks, manifest.

Usage:
  .venv/bin/python -m harness.validate_capture_session <session_dir>
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.data.capture import validate_capture_session


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python -m harness.validate_capture_session <session_dir>", file=sys.stderr)
        sys.exit(2)

    session_dir = Path(sys.argv[1])
    if not session_dir.is_dir():
        print(f"Not a directory: {session_dir}", file=sys.stderr)
        sys.exit(2)

    passed, result = validate_capture_session(session_dir)

    print("Capture Validation:", "PASS" if passed else "FAIL")
    print(f"Frames: {result['frames']}")
    print(f"Frames with hands: {result['frames_with_hands']}")
    print(f"Frames with detections: {result['frames_with_detections']}")
    print(f"Machine-labelled events: {result['machine_labelled_events']}")
    print(f"Missing/invalid records: {result['missing_or_invalid']}")
    print(f"Raw video: {result['raw_video']}")

    if result["errors"]:
        for e in result["errors"][:10]:
            print(f"  ERROR: {e}")

    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
