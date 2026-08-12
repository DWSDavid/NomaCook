"""Interactive human review CLI for capture session review items.

Usage:
  .venv/bin/python -m harness.review_capture_session <session_dir> [--open-clips]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.data.review import apply_review, build_label_metrics, _read_jsonl


def _write_jsonl_atomic(path: Path, rows: list[dict]) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp.replace(path)


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python -m harness.review_capture_session <session_dir> [--open-clips]",
              file=sys.stderr)
        sys.exit(2)

    session_dir = Path(sys.argv[1])
    review_dir = session_dir / "review"
    open_clips = "--open-clips" in sys.argv

    queue_path = review_dir / "review_queue.jsonl"
    labels_path = review_dir / "gold_labels.jsonl"

    if not queue_path.exists():
        print(f"No review queue found. Run build_review_queue first.", file=sys.stderr)
        sys.exit(1)

    queue = _read_jsonl(queue_path)
    labels = _read_jsonl(labels_path) if labels_path.exists() else []
    label_map = {g["review_item_id"]: g for g in labels}

    total = len(queue)
    reviewed_count = sum(1 for g in labels if g.get("reviewer_label") is not None)
    print(f"Session: {session_dir}")
    print(f"Queue items: {total}  Reviewed: {reviewed_count}  Remaining: {total - reviewed_count}")
    print("[c] correct  [i] incorrect  [u] uncertain  [s] skip  [q] quit")
    print()

    for idx, item in enumerate(queue):
        rid = item["review_item_id"]
        gl = label_map.get(rid, {})
        if gl.get("reviewer_label") is not None:
            continue  # already reviewed

        reason = item["reason"]
        step = item.get("machine_label", {}).get("predicted_step_id", "")
        event_type = item.get("machine_label", {}).get("event_type", "")
        conf = item.get("machine_label", {}).get("confidence", "")

        display = f"[{idx + 1}/{total}] {reason}"
        if step:
            display += f" → {step}"
        if event_type:
            display += f" ({event_type}"
            if conf:
                display += f", conf={conf:.2f}"
            display += ")"

        clip_abs = str(review_dir / item.get("clip_path", ""))
        print(display)
        print(f"  Clip: {clip_abs}")

        if open_clips:
            import subprocess
            subprocess.run(["open", clip_abs], check=False)

        try:
            choice = input("\nLabel: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nQuit.")
            break

        if choice == "q":
            print("Quit.")
            break
        elif choice == "s":
            continue
        elif choice == "c":
            new_gl = apply_review(gl, reviewer_label="correct",
                                   event_type=event_type or reason.upper(),
                                   step_after=step or None)
        elif choice == "i":
            new_et = input("  Correct event_type (blank for none): ").strip() or None
            new_step = input("  Correct step_after (blank for none): ").strip() or None
            new_gl = apply_review(gl, reviewer_label="incorrect",
                                   event_type=new_et, step_after=new_step)
        elif choice == "u":
            new_gl = apply_review(gl, reviewer_label="uncertain")
        else:
            print("  Unknown choice, skipping.")
            continue

        label_map[rid] = new_gl
        # atomic write
        all_labels = list(label_map.values())
        _write_jsonl_atomic(labels_path, all_labels)
        item["review_status"] = "reviewed"
        _write_jsonl_atomic(queue_path, queue)
        reviewed_count = sum(1 for g in all_labels if g.get("reviewer_label") is not None)
        print(f"  Saved. Reviewed: {reviewed_count}/{total}")

    # final summary
    final_labels = _read_jsonl(labels_path) if labels_path.exists() else []
    reviewed = [g for g in final_labels if g.get("reviewer_label") is not None]
    golds = [g for g in final_labels if g.get("is_ground_truth")]
    uncerts = [g for g in final_labels if g.get("reviewer_label") == "uncertain"]
    summary_path = review_dir / "review_summary.json"
    if summary_path.exists():
        summary = json.loads(summary_path.read_text())
        summary["gold_labels"] = len(golds)
        summary["metrics"] = build_label_metrics(session_dir, review_dir)
        tmp = summary_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
        tmp.replace(summary_path)
    print(f"\nDone. Reviewed: {len(reviewed)}  Gold: {len(golds)}  Uncertain: {len(uncerts)}")


if __name__ == "__main__":
    main()
