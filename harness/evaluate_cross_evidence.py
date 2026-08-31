"""Cross-evidence evaluator CLI.

Usage:
  .venv/bin/python -m harness.evaluate_cross_evidence \
    <session_dir> [<session_dir> ...] \
    --output <path> [--vlm-shadow] [--contact-sheet-dir <dir>]

Deterministic evaluation always runs. --vlm-shadow runs the offline Qwen VLM
shadow cross-check when DASHSCOPE_API_KEY + BAILIAN_WORKSPACE_ID are present;
otherwise it emits status=skipped and the deterministic evaluator still
succeeds. No fallback to Gemini.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.data.cross_evidence import (
    build_report,
    evaluate_session,
    run_shadow_evaluation,
    summarize_shadow,
)
from server.data.capture import secrets_check
from server.vlm.client import qwen_vlm_configured


def _run_vlm_shadow(
    session_results: list[dict[str, Any]],
    contact_sheet_dir: Path | None,
) -> list[dict[str, Any]]:
    """Run offline Qwen VLM shadow evaluation. Never touches live state."""
    if not qwen_vlm_configured():
        return [{
            "status": "skipped",
            "reason": "no DASHSCOPE_API_KEY / BAILIAN_WORKSPACE_ID configured",
        }]

    from server.vlm.client import QwenVLMClient

    client = QwenVLMClient()
    return run_shadow_evaluation(
        session_results, client, contact_sheet_dir=contact_sheet_dir,
    )


def main() -> None:
    args = sys.argv[1:]
    if "--output" not in args:
        print("Usage: python -m harness.evaluate_cross_evidence "
              "<session_dir> [...] --output <path> [--vlm-shadow] "
              "[--contact-sheet-dir <dir>]", file=sys.stderr)
        sys.exit(2)

    out_idx = args.index("--output")
    output_path = Path(args[out_idx + 1])
    session_dirs = [Path(a) for a in args[:out_idx] if not a.startswith("--")]
    vlm_shadow = "--vlm-shadow" in args

    contact_sheet_dir = None
    if "--contact-sheet-dir" in args:
        cs_idx = args.index("--contact-sheet-dir")
        contact_sheet_dir = Path(args[cs_idx + 1])

    if not session_dirs:
        print("At least one session directory is required", file=sys.stderr)
        sys.exit(2)

    for d in session_dirs:
        if not d.is_dir():
            print(f"Not a directory: {d}", file=sys.stderr)
            sys.exit(2)

    # secrets scan on inputs
    for d in session_dirs:
        for fname in ("observations.jsonl", "events.jsonl"):
            p = d / fname
            if p.exists():
                secrets_check(p.read_text())

    session_results = [evaluate_session(d) for d in session_dirs]

    shadow = _run_vlm_shadow(session_results, contact_sheet_dir) if vlm_shadow else []

    report = build_report(session_results, vlm_shadow=shadow)
    if vlm_shadow:
        report["shadow_summary"] = summarize_shadow(shadow)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))

    m = report["metrics"]
    print(f"Cross-Evidence Evaluation: {report['schema_version']}")
    print(f"Sessions: {len(session_results)}")
    print(f"Gold items: {m['gold_count']}")
    print(f"  correct: {m['correct']}  incorrect: {m['incorrect']}")
    print(f"Uncertain items: {m['uncertain_count']}")
    print(f"Accuracy: {m['accuracy']}")
    print(f"Sample status: {m['sample_status']}")
    if vlm_shadow:
        ss = report.get("shadow_summary", {})
        print(f"Qwen shadow: {ss.get('executed')} executed, "
              f"{ss.get('skipped')} skipped, {ss.get('errors')} errors")
        if ss.get("model"):
            print(f"  provider={ss.get('provider')} model={ss.get('model')} "
                  f"region={ss.get('region')}")
            print(f"  latency p50={ss.get('latency_p50_ms')}ms "
                  f"p95={ss.get('latency_p95_ms')}ms")
            print(f"  answer_distribution={ss.get('answer_distribution')}")
            print(f"  gold_comparable={ss.get('gold_comparable_count')} "
                  f"agreement_rate={ss.get('agreement_rate')}")
    print(f"Report: {output_path}")


if __name__ == "__main__":
    main()
