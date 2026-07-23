from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
VIDEO = REPO / "data" / "test_videos" / "synthetic_smoke.mp4"
SCRIPT = REPO / "tests" / "fixtures" / "tomato_egg_full_script.json"
PY = REPO / ".venv" / "bin" / "python"


def _run(tmp_out: str) -> Path:
    import shutil
    for stale in (REPO / "data" / "sessions").glob(f"*synthetic_smoke*/run_{tmp_out}"):
        shutil.rmtree(stale, ignore_errors=True)  # leftovers from failed runs
    cmd = [str(PY), "harness/run_pipeline.py",
           "--source", str(VIDEO), "--device", "cpu",
           "--script", str(SCRIPT), "--run-tag", tmp_out,
           "--max-frames", "90", "--keyframe-interval", "1.0"]
    subprocess.run(cmd, cwd=REPO, check=True, capture_output=True, text=True)
    session_dir = next((REPO / "data" / "sessions").glob("*synthetic_smoke*"))
    return session_dir / f"run_{tmp_out}"


@pytest.mark.e2e
def test_full_pipeline_is_deterministic_and_produces_all_artifacts():
    if not VIDEO.is_file():
        pytest.skip("synthetic_smoke.mp4 missing")
    left = _run("e2e_left")
    right = _run("e2e_right")
    try:
        compare = subprocess.run(
            [str(PY), "-m", "server.events.replay", "compare",
             str(left / "events.jsonl"), str(right / "events.jsonl")],
            cwd=REPO, capture_output=True, text=True)
        assert compare.returncode == 0, compare.stderr
        assert "equal" in compare.stdout

        meta = json.loads((left / "meta.json").read_text())
        assert meta["final_status"] == "completed"
        assert len(meta["transitions"]) == 4
        assert meta["annotated_frames"] == meta["frames"]
        assert (left / "annotated.mp4").stat().st_size > 0
        timeline_rows = (left / "timeline.jsonl").read_text().splitlines()
        assert len(timeline_rows) >= 2
        assert len(list((left / "keyframes").glob("*.jpg"))) == len(timeline_rows)
    finally:
        import shutil
        shutil.rmtree(left, ignore_errors=True)
        shutil.rmtree(right, ignore_errors=True)
