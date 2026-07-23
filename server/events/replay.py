"""Validate, normalize, and compare NomaChef event JSONL streams."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import sys
from typing import Iterable

from .log import read_events
from .schema import EventEnvelope


@dataclass(frozen=True)
class ReplayDiff:
    equal: bool
    index: int | None = None
    left: str | None = None
    right: str | None = None


def replay(events: Iterable[EventEnvelope]) -> list[EventEnvelope]:
    """Produce the canonical session order and reject ambiguous sequence numbers."""

    ordered = sorted(events, key=lambda event: (event.seq, event.event_id))
    seen_seq: set[int] = set()
    for event in ordered:
        if event.seq in seen_seq:
            raise ValueError(f"duplicate seq {event.seq} in replay stream")
        seen_seq.add(event.seq)
    return ordered


def canonical_lines(
    events: Iterable[EventEnvelope], *, ignore_received_at: bool = True
) -> list[str]:
    return [
        json.dumps(
            event.canonical_dict(include_received_at=not ignore_received_at),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        for event in replay(events)
    ]


def compare_logs(
    left_path: str | Path,
    right_path: str | Path,
    *,
    ignore_received_at: bool = True,
) -> ReplayDiff:
    left_lines = canonical_lines(
        read_events(left_path), ignore_received_at=ignore_received_at
    )
    right_lines = canonical_lines(
        read_events(right_path), ignore_received_at=ignore_received_at
    )
    for index in range(max(len(left_lines), len(right_lines))):
        left = left_lines[index] if index < len(left_lines) else None
        right = right_lines[index] if index < len(right_lines) else None
        if left != right:
            return ReplayDiff(False, index=index, left=left, right=right)
    return ReplayDiff(True)


def write_canonical(source: str | Path, destination: str | Path) -> int:
    lines = canonical_lines(read_events(source))
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as stream:
        for line in lines:
            stream.write(line + "\n")
    return len(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate and replay one log")
    validate.add_argument("path", type=Path)

    normalize = subparsers.add_parser("normalize", help="write canonical seq order")
    normalize.add_argument("source", type=Path)
    normalize.add_argument("destination", type=Path)

    compare = subparsers.add_parser("compare", help="compare two canonical streams")
    compare.add_argument("left", type=Path)
    compare.add_argument("right", type=Path)
    compare.add_argument(
        "--include-received-at",
        action="store_true",
        help="treat backend receive-time differences as meaningful",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "validate":
        events = replay(read_events(args.path))
        print(f"ok: {len(events)} events")
        return
    if args.command == "normalize":
        count = write_canonical(args.source, args.destination)
        print(f"wrote {count} events to {args.destination}")
        return

    diff = compare_logs(
        args.left,
        args.right,
        ignore_received_at=not args.include_received_at,
    )
    if diff.equal:
        print("equal")
        return
    print(f"different at replay index {diff.index}", file=sys.stderr)
    print(f"left:  {diff.left}", file=sys.stderr)
    print(f"right: {diff.right}", file=sys.stderr)
    raise SystemExit(1)


if __name__ == "__main__":
    main()
