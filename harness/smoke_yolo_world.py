"""Smoke test: YOLO-World open-vocabulary detection with a kitchen vocabulary.

Usage:
    python harness/smoke_yolo_world.py [image_or_video_path]

Without an argument it falls back to the ultralytics sample image (bus.jpg)
just to prove the pipeline runs end to end. Drop real first-person kitchen
frames into data/test_frames/ and pass them here.
"""

import sys
import time
from pathlib import Path

from ultralytics import YOLOWorld

# Kitchen vocabulary for a fried-rice-class dish. This is the per-step
# objects_involved list from the SOP, in English (YOLO-World's text encoder
# is English CLIP; SOP schema should store English object names).
KITCHEN_VOCAB = [
    "wok",
    "frying pan",
    "spatula",
    "cutting board",
    "kitchen knife",
    "bowl",
    "plate",
    "egg",
    "soy sauce bottle",
    "oil bottle",
    "scallion",
    "garlic",
    "rice",
    "hand",
]

OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "test_frames"


def main() -> None:
    source = sys.argv[1] if len(sys.argv) > 1 else "https://ultralytics.com/images/bus.jpg"

    model = YOLOWorld("yolov8s-worldv2.pt")
    model.set_classes(KITCHEN_VOCAB)

    t0 = time.perf_counter()
    results = model.predict(source, conf=0.15, device="mps", verbose=False)
    dt = time.perf_counter() - t0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for i, r in enumerate(results):
        names = r.names
        print(f"--- frame {i} | {len(r.boxes)} detections | {dt*1000:.0f} ms total ---")
        for box in r.boxes:
            cls = names[int(box.cls)]
            conf = float(box.conf)
            xyxy = [round(v) for v in box.xyxy[0].tolist()]
            print(f"  {cls:<18} conf={conf:.2f} box={xyxy}")
        out = OUT_DIR / f"smoke_annotated_{i}.jpg"
        r.save(str(out))
        print(f"  annotated -> {out}")


if __name__ == "__main__":
    main()
