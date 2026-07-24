"""Offline probe: FastSAM segmentation as a chopping-quality signal.

IngredSAM-lite: segment everything with FastSAM (already in ultralytics, zero
new deps), classify masks by HSV color (tomato-red / egg-yellow), and report
piece count + area stats per keyframe. Runs OFFLINE on a previous run's
keyframes — never in the realtime loop. If the metrics look sane on real
footage, the output graduates to a weight-0.2 evidence rule for step_03;
if the masks are garbage, we drop the idea with evidence.

Usage:
    .venv/bin/python harness/probe_seg.py \
        --run-dir data/sessions/<session>/<run>/          # eats keyframes/
    .venv/bin/python harness/probe_seg.py --image path.jpg
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_REPO_ROOT = Path(__file__).resolve().parent.parent
WEIGHTS = _REPO_ROOT / "weights" / "FastSAM-s.pt"

# Piece-size band as fraction of frame area: below = noise/specks,
# above = whole tomato / wok / cutting board, not a chopped piece.
MIN_PIECE_FRAC = 0.0005
MAX_PIECE_FRAC = 0.03


def classify_mask_color(hsv_mean: tuple[float, float, float]) -> str | None:
    """Map a mask's mean HSV (OpenCV ranges: H 0-180) to an ingredient color.

    Pure function so the thresholds are unit-testable without FastSAM.
    """
    h, s, v = hsv_mean
    if s < 70 or v < 60:
        return None  # washed-out: countertop, steel, shadow
    if h <= 12 or h >= 168:
        return "tomato_red"
    if 18 <= h <= 38:
        return "egg_yellow"
    return None


def piece_metrics(
    masks: np.ndarray, frame_bgr: np.ndarray
) -> list[dict]:
    """Per-mask color + area stats. masks: (N, H, W) bool/0-1 array."""
    h, w = frame_bgr.shape[:2]
    frame_area = float(h * w)
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
    rows: list[dict] = []
    for mask in masks:
        m = mask.astype(bool)
        area = float(m.sum())
        frac = area / frame_area
        if not MIN_PIECE_FRAC <= frac <= MAX_PIECE_FRAC:
            continue
        mean = hsv[m].mean(axis=0)
        color = classify_mask_color((float(mean[0]), float(mean[1]), float(mean[2])))
        if color is None:
            continue
        ys, xs = np.nonzero(m)
        rows.append(
            {
                "color": color,
                "area_frac": round(frac, 5),
                "box": [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())],
            }
        )
    return rows


def summarize(rows: list[dict]) -> dict:
    out: dict = {}
    for color in ("tomato_red", "egg_yellow"):
        pieces = [r for r in rows if r["color"] == color]
        areas = sorted(r["area_frac"] for r in pieces)
        out[color] = {
            "count": len(pieces),
            "median_area_frac": areas[len(areas) // 2] if areas else 0.0,
            "total_area_frac": round(sum(areas), 5),
        }
    return out


def probe_image(model, image_path: Path, out_dir: Path, device: str) -> dict:
    frame = cv2.imread(str(image_path))
    if frame is None:
        return {"image": image_path.name, "error": "unreadable"}
    t0 = time.perf_counter()
    results = model(
        frame, device=device, retina_masks=True, imgsz=640,
        conf=0.4, iou=0.9, verbose=False,
    )
    latency_ms = (time.perf_counter() - t0) * 1000
    r = results[0]
    if r.masks is None:
        return {"image": image_path.name, "latency_ms": round(latency_ms),
                "pieces": [], "summary": summarize([])}
    masks = r.masks.data.cpu().numpy()
    rows = piece_metrics(masks, frame)

    overlay = frame.copy()
    palette = {"tomato_red": (0, 0, 255), "egg_yellow": (0, 200, 255)}
    for row in rows:
        x1, y1, x2, y2 = row["box"]
        cv2.rectangle(overlay, (x1, y1), (x2, y2), palette[row["color"]], 2)
    summary = summarize(rows)
    label = (f"red x{summary['tomato_red']['count']} "
             f"yellow x{summary['egg_yellow']['count']}")
    cv2.putText(overlay, label, (8, 24), cv2.FONT_HERSHEY_SIMPLEX,
                0.7, (255, 255, 255), 2)
    cv2.imwrite(str(out_dir / f"seg_{image_path.name}"), overlay)

    return {"image": image_path.name, "latency_ms": round(latency_ms),
            "pieces": rows, "summary": summary}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-dir", help="pipeline run dir containing keyframes/")
    ap.add_argument("--image", help="single image instead of a run dir")
    ap.add_argument("--device", default="mps")
    args = ap.parse_args()

    from ultralytics import FastSAM  # deferred: heavy import

    WEIGHTS.parent.mkdir(parents=True, exist_ok=True)
    model = FastSAM(str(WEIGHTS))  # auto-downloads ~23MB on first use

    if args.image:
        images = [Path(args.image)]
        out_dir = Path(args.image).resolve().parent / "probe_seg"
    elif args.run_dir:
        keyframes = Path(args.run_dir) / "keyframes"
        if not keyframes.is_dir():
            raise SystemExit(f"no keyframes/ under {args.run_dir}")
        images = sorted(keyframes.glob("*.jpg"))
        out_dir = Path(args.run_dir) / "probe_seg"
    else:
        raise SystemExit("pass --run-dir or --image")

    out_dir.mkdir(exist_ok=True)
    report_path = out_dir / "metrics.jsonl"
    with report_path.open("w") as fh:
        for image_path in images:
            row = probe_image(model, image_path, out_dir, args.device)
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            s = row.get("summary", {})
            print(f"{row['image']}: "
                  f"red={s.get('tomato_red', {}).get('count', '?')} "
                  f"yellow={s.get('egg_yellow', {}).get('count', '?')} "
                  f"({row.get('latency_ms', '?')} ms)")
    print(f"\noverlays + metrics -> {out_dir}")


if __name__ == "__main__":
    main()
