"""Render one run's meta + timeline into a human-readable report.md."""

from __future__ import annotations

import json
from pathlib import Path

from .session import SessionPaths


def write_report(paths: SessionPaths) -> Path:
    meta = json.loads(paths.meta.read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in
            paths.timeline.read_text(encoding="utf-8").splitlines() if line.strip()]
    vlm_rows = []
    if paths.events.exists():
        for line in paths.events.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if str(event.get("type", "")).startswith("vlm.step_assessment"):
                vlm_rows.append(event)

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
    lines += ["", "## Gemini observations", ""]
    if vlm_rows:
        lines += [
            "| pts | step | phase | conf | risk | reason | coach_comment | frame |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for event in vlm_rows:
            payload = event.get("payload", {})
            frame_id = payload.get("frame_id")
            vlm_jpg = f"vlm_{frame_id}.jpg" if frame_id else None
            frame_text = (
                f"`keyframes/{vlm_jpg}`"
                if vlm_jpg and (paths.keyframes_dir / vlm_jpg).exists()
                else str(frame_id or "-")
            )
            lines.append(
                f"| {event.get('t_device_ms', 0):.0f}ms "
                f"| {payload.get('step_id', '-')} "
                f"| {payload.get('phase', '-')} "
                f"| {event.get('confidence') or 0:.2f} "
                f"| {payload.get('risk_level', '-')} "
                f"| {payload.get('reason', '-')} "
                f"| {payload.get('coach_comment') or '-'} "
                f"| {frame_text} |")
    else:
        lines.append("(no Gemini VLM calls in this run — check GEMINI_API_KEY / --vlm)")
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
