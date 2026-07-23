"""Render one run's meta + timeline into a human-readable report.md."""

from __future__ import annotations

import json
from pathlib import Path

from .session import SessionPaths


def write_report(paths: SessionPaths) -> Path:
    meta = json.loads(paths.meta.read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in
            paths.timeline.read_text(encoding="utf-8").splitlines() if line.strip()]

    lines: list[str] = [
        f"# NomaChef Run Report: {meta['session_id']}",
        "",
        f"- video: `{meta['video']}` | sop: `{meta['sop']}` | fps: {meta['fps']:.1f}",
        f"- frames: {meta['frames']} | events: {meta['events']} | "
        f"vlm: {meta.get('vlm_mode', 'off')}",
        f"- final: **{meta['final_status']}** at `{meta['final_step_id']}`",
        "",
        "## Step transitions",
        "",
        "| pts | completed | next | score |",
        "|---|---|---|---|",
    ]
    for tr in meta.get("transitions", []):
        lines.append(
            f"| {tr['pts_ms']:.0f}ms | {tr['completed_step_id']} | "
            f"{tr['next_step_id'] or 'END'} | {tr['score']:.2f} |")
    lines += ["", "## Timeline keyframes", "",
              "| pts | step | score | color | detections | diff | frame |",
              "|---|---|---|---|---|---|---|"]
    for row in rows:
        dets = ", ".join(f"{label}:{conf:.2f}" for label, conf in row["detections"])
        diff = row["diff"]
        diff_text = ("baseline" if diff.get("baseline") else
                     "; ".join(f"{k}={v}" for k, v in diff.items() if v))
        lines.append(
            f"| {row['pts_ms']:.0f}ms | {row['step_id']} | {row['score']:.2f} | "
            f"{row['color_state'] or '-'} | {dets or '-'} | {diff_text or '-'} | "
            f"`keyframes/{row['jpg']}` |")
    paths.report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return paths.report
