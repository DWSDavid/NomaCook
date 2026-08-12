"""Cross-evidence evaluator CLI.

Usage:
  .venv/bin/python -m harness.evaluate_cross_evidence \
    <session_dir> [<session_dir> ...] \
    --output <path> [--vlm-shadow]

Deterministic evaluation always runs. --vlm-shadow runs the offline VLM
shadow cross-check when a GEMINI_API_KEY is configured; otherwise it emits
status=skipped and the deterministic evaluator still succeeds.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.data.cross_evidence import (
    build_report,
    evaluate_session,
    run_shadow_evaluation,
)
from server.data.capture import secrets_check
from server.gemini_config import gemini_is_configured


def _run_vlm_shadow(session_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Run offline VLM shadow evaluation, returning records. Never touches live state."""
    if not gemini_is_configured():
        return [{
            "status": "skipped",
            "reason": "no GEMINI_API_KEY configured",
        }]

    from server.vlm.client import GeminiVLMClient

    client = GeminiVLMClient()
    try:
        return run_shadow_evaluation(session_results, client)
    finally:
        client.close()


def main() -> None:
    args = sys.argv[1:]
    if "--output" not in args:
        print("Usage: python -m harness.evaluate_cross_evidence "
              "<session_dir> [...] --output <path> [--vlm-shadow]", file=sys.stderr)
        sys.exit(2)

    out_idx = args.index("--output")
    output_path = Path(args[out_idx + 1])
    session_dirs = [Path(a) for a in args[:out_idx] if not a.startswith("--")]
    vlm_shadow = "--vlm-shadow" in args

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

    shadow = _run_vlm_shadow(session_results) if vlm_shadow else []

    report = build_report(session_results, vlm_shadow=shadow)

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
        executed = [r for r in shadow if r.get("status") == "executed"]
        skipped = [r for r in shadow if r.get("status") == "skipped"]
        print(f"VLM shadow: {len(executed)} executed, {len(skipped)} skipped")
    print(f"Report: {output_path}")


if __name__ == "__main__":
    main()
