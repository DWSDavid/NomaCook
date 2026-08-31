# Tomato Eval Bilingual Overlay and Local Voice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a clearly legible bilingual tomato-task evaluation video with local Chinese phase-transition narration and explicit next-step instructions.

**Architecture:** Keep StateEngine and perception unchanged. Add a task-specific presentation layer inside the offline evaluator that derives bilingual display copy and Chinese narration only from recognized StateEngine transitions and the SOP. Reuse the existing Pillow rendering pattern, macOS `say`, narration scheduler, and FFmpeg muxer.

**Tech Stack:** Python, OpenCV, Pillow, macOS `say` with `Meijia`, FFmpeg, pytest.

## Global Constraints

- Human annotations remain evaluation-only and never affect prediction, instructions, or speech.
- Use local `Meijia`; no Tingting, iFlytek, or network API.
- Speech occurs only for meaningful phase transitions.
- The final narrated artifact is `annotated_narrated.mp4` with AAC audio.
- Preserve all unrelated dirty worktree changes.

---

### Task 1: Bilingual presentation and narration copy

**Files:**
- Modify: `harness/eval_tomato_to_fridge.py`
- Create: `tests/test_eval_tomato_to_fridge_presentation.py`

**Interfaces:**
- Produces: `_presentation_for(recognized_step_id: str, next_step_id: str | None) -> dict[str, str]`
- Produces: `_local_narration_items(transitions: list[dict]) -> list[dict]`

- [x] **Step 1: Write failing presentation tests**

```python
def test_presentation_contains_bilingual_recognized_and_next_copy():
    presentation = evaluator._presentation_for("tomato_held", "tomato_in_transit")
    assert presentation["recognized_zh"] == "已拿起番茄"
    assert presentation["recognized_en"] == "Tomato picked up"
    assert presentation["next_zh"] == "请把番茄移向冰箱。"
    assert presentation["next_en"] == "Move the tomato toward the refrigerator."

def test_local_narration_keeps_only_meaningful_phase_changes():
    transitions = [
        {"step_id": step_id, "pts_ms": index * 1000.0}
        for index, step_id in enumerate([
            "ready", "tomato_on_table", "hand_near_tomato", "tomato_held",
            "tomato_in_transit", "fridge_interaction",
            "candidate_inside_fridge", "tomato_released_inside",
        ])
    ]
    items = evaluator._local_narration_items(transitions)
    assert [item["step_id"] for item in items] == [
        "tomato_on_table", "tomato_held", "fridge_interaction",
        "candidate_inside_fridge", "tomato_released_inside",
    ]
```

- [x] **Step 2: Run tests and verify the missing helpers fail**

Run: `.venv/bin/python -m pytest tests/test_eval_tomato_to_fridge_presentation.py -q`

Expected: failing assertions because the presentation and narration helpers do not exist.

- [x] **Step 3: Add the minimum task copy and pure helpers**

Use literal bilingual copy for the eight task phases. Narration items must contain `pts_ms`, `kind`, `text`, and `step_id`; select only five meaningful spoken transitions so adjacent early states do not create overlapping speech.

- [x] **Step 4: Run the focused tests**

Run: `.venv/bin/python -m pytest tests/test_eval_tomato_to_fridge_presentation.py -q`

Expected: all tests pass.

### Task 2: High-contrast bilingual rendering

**Files:**
- Modify: `harness/eval_tomato_to_fridge.py`
- Test: `tests/test_eval_tomato_to_fridge_presentation.py`

**Interfaces:**
- Consumes: `_presentation_for(recognized_step_id, next_step_id)`
- Produces: `_draw_status_panel(frame, presentation, debug_text) -> None`

- [x] **Step 1: Write a failing rendering test**

```python
def test_overlay_draws_large_dark_header_and_debug_footer():
    frame = np.full((1080, 1920, 3), 255, dtype=np.uint8)
    presentation = evaluator._presentation_for("tomato_held", "tomato_in_transit")
    evaluator._draw_status_panel(frame, presentation, "YOLO 21ms | score 0.5/0.8")
    assert frame[:170].mean() < 90
    assert frame[-36:].mean() < 90
```

- [x] **Step 2: Verify it fails against the existing 72-pixel overlay**

Run: `.venv/bin/python -m pytest tests/test_eval_tomato_to_fridge_presentation.py::test_overlay_draws_large_dark_header_and_debug_footer -q`

Expected: FAIL because the current header is too small and no debug footer exists.

- [x] **Step 3: Implement the approved layout**

Use Pillow with `/System/Library/Fonts/Hiragino Sans GB.ttc` for Chinese and Latin text. Draw a roughly 180-pixel black header split into green `已识别 / RECOGNIZED` and amber `下一步 / NEXT` areas. Increase detection boxes to four pixels, hand lines to three pixels, and move score/latency/hits into a small dark footer.

- [x] **Step 4: Run focused rendering tests**

Run: `.venv/bin/python -m pytest tests/test_eval_tomato_to_fridge_presentation.py -q`

Expected: all tests pass.

### Task 3: Local Meijia narration and v4 acceptance

**Files:**
- Modify: `harness/eval_tomato_to_fridge.py`
- Test: `tests/test_eval_tomato_to_fridge_presentation.py`
- Output: `data/evals/IMG_9789_table_to_fridge_v4/annotated_narrated.mp4`

**Interfaces:**
- Adds CLI: `--local-narration`
- Adds CLI: `--voice`, default `Meijia`
- Reuses: `server.pipeline.narrate.narrate_run(run_root: Path, backend="say", voice="Meijia")`

- [x] **Step 1: Write failing CLI and narration-artifact tests**

```python
def test_parser_defaults_to_meijia():
    args = evaluator.build_parser().parse_args(["--source", "x", "--run-dir", "y"])
    assert args.voice == "Meijia"
    assert args.local_narration is False
```

- [x] **Step 2: Verify the parser test fails**

Run: `.venv/bin/python -m pytest tests/test_eval_tomato_to_fridge_presentation.py::test_parser_defaults_to_meijia -q`

Expected: FAIL because the CLI options do not exist.

- [x] **Step 3: Add local narration output**

After closing `annotated.mp4`, write `_local_narration_items(predicted_transitions)` to `narration.json`. When `--local-narration` is enabled, reject runs without `--render-video`, then call `narrate_run(run_dir, backend="say", voice=args.voice)` and record the narrated path in `summary.json`.

- [x] **Step 4: Run focused and full tests**

Run: `.venv/bin/python -m pytest tests/test_eval_tomato_to_fridge_presentation.py -q`

Run: `.venv/bin/python -m pytest -q -m 'not e2e'`

Expected: all focused tests pass; the full suite has zero failures.

- [x] **Step 5: Render and verify v4**

Run:

```bash
.venv/bin/python -m harness.eval_tomato_to_fridge \
  --source data/test_videos/IMG_9789_table_to_fridge.MOV \
  --run-dir data/evals/IMG_9789_table_to_fridge_v4 \
  --annotations data/annotations/IMG_9789_table_to_fridge.yaml \
  --render-video --local-narration --voice Meijia
```

Verify `summary.json` reports `completed` and at least `0.90` frame match rate. Verify `ffprobe` reports an AAC audio stream for `annotated_narrated.mp4`, and inspect representative frames at 0.5, 3.8, 13.5, 22.5, and 24.5 seconds.

- [x] **Step 6: Commit only audited implementation files**

```bash
git add harness/eval_tomato_to_fridge.py tests/test_eval_tomato_to_fridge_presentation.py docs/superpowers/plans/2026-08-10-tomato-eval-bilingual-overlay-local-voice.md
git commit -m "feat: add bilingual narrated tomato evaluation"
```
