"""Evaluate the kitchen vocabulary against fixture frames.

Track C (runbook Step C1). Runs YOLO-World with the full kitchen vocab over
every image in data/test_frames/fixtures/ (or a directory you pass) and
reports, per vocabulary term: frames hit, detection count, confidence
distribution. Terms that never fire on your own footage are candidates for
rephrasing or removal; strong terms inform the per-step conf threshold.

Usage:
    .venv/bin/python harness/eval_vocab.py                # fixtures dir
    .venv/bin/python harness/eval_vocab.py path/to/frames --conf 0.05

Report prints to stdout and lands as JSON next to the frames.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from perception.detector import ObjectDetector
from perception.kitchen_vocab import category_of, full_vocab

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FRAMES_DIR = REPO_ROOT / "data" / "test_frames" / "fixtures"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "frames_dir", nargs="?", default=str(DEFAULT_FRAMES_DIR),
        help="directory of evaluation frames",
    )
    parser.add_argument(
        "--conf", type=float, default=0.05,
        help="low threshold on purpose: we want the full confidence distribution",
    )
    parser.add_argument("--device", default="mps")
    args = parser.parse_args()

    frames_dir = Path(args.frames_dir)
    frames = sorted(
        p for p in frames_dir.glob("*") if p.suffix.lower() in IMAGE_SUFFIXES
    )
    if not frames:
        sys.exit(
            f"no frames in {frames_dir} — record self-captured video first "
            "(Step 0 leftover) and extract fixture frames there."
        )

    import cv2

    vocab = full_vocab()
    detector = ObjectDetector(vocab=vocab, device=args.device, conf=args.conf)
    print(f"{len(frames)} frames | {len(vocab)} vocab terms | conf>={args.conf}")

    # term -> list of (frame_name, conf)
    hits: dict[str, list[tuple[str, float]]] = defaultdict(list)
    latencies: list[float] = []
    for path in frames:
        frame = cv2.imread(str(path))
        if frame is None:
            print(f"  skip unreadable {path.name}")
            continue
        for det in detector.detect(frame):
            hits[det.label].append((path.name, det.conf))
        latencies.append(detector.last_latency_ms)

    rows = []
    for term in vocab:
        term_hits = hits.get(term, [])
        confs = [c for _, c in term_hits]
        rows.append(
            {
                "term": term,
                "category": category_of(term),
                "frames_hit": len({f for f, _ in term_hits}),
                "detections": len(term_hits),
                "conf_max": round(max(confs), 3) if confs else 0.0,
                "conf_mean": round(sum(confs) / len(confs), 3) if confs else 0.0,
            }
        )
    rows.sort(key=lambda r: (-r["frames_hit"], -r["conf_max"]))

    print(f"\n{'term':<22} {'category':<12} {'frames':>6} {'dets':>5} "
          f"{'max':>5} {'mean':>5}")
    for r in rows:
        flag = "" if r["frames_hit"] else "  <- never fires"
        print(f"{r['term']:<22} {r['category']:<12} {r['frames_hit']:>6} "
              f"{r['detections']:>5} {r['conf_max']:>5.2f} {r['conf_mean']:>5.2f}{flag}")

    dead = [r["term"] for r in rows if not r["frames_hit"]]
    if latencies:
        avg_ms = sum(latencies) / len(latencies)
        print(f"\navg detect latency: {avg_ms:.0f} ms/frame")
    print(f"never-firing terms ({len(dead)}): {', '.join(dead) or 'none'}")

    report = {
        "frames_dir": str(frames_dir),
        "n_frames": len(frames),
        "conf_threshold": args.conf,
        "avg_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else None,
        "terms": rows,
    }
    out = frames_dir / "vocab_eval_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"report -> {out}")


if __name__ == "__main__":
    main()
