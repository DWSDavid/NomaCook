# NomaChef

> **Mom, I cooked.** 「妈,我会做饭了。」

A chest-worn, real-time cooking copilot: a wide-angle camera watches your first-person view, voice guides you step by step, and a multi-signal perception stack verifies each step is actually done. Design principle: **verify outcome states, never recognize actions.**

Cooking is the wedge. The engine generalizes to any hands-on procedural task (accessibility, eldercare, lab protocols, assembly & repair, STEM education).

## Read first

**[CLAUDE.md](CLAUDE.md)** is the full project context: architecture, three-layer perception design, model choices, SOP schema, state engine, hardware, and hackathon priorities. All agents (Claude Code / Codex / OpenCode) and humans start there.

- Hardware shopping list: [docs/hardware-bom.md](docs/hardware-bom.md)

## Repo layout

```
perception/   Layer 1-3 perception (YOLO-World, MediaPipe, audio, VLM checks)
server/       Event log, state engine, VLM, pipeline orchestration, and voice
sop/          SOP JSON schema + hand-written recipes
harness/      Offline replay harness: run the full stack against recorded video
data/         Test videos & frames (gitignored)
docs/         BOM and other docs
```

## Dev setup (macOS)

```bash
uv venv --python 3.12 .venv
uv pip install -p .venv -r requirements.txt
uv pip install -p .venv "git+https://github.com/ultralytics/CLIP.git"
.venv/bin/python harness/smoke_yolo_world.py   # YOLO-World kitchen-vocab smoke test
```

Benchmarks so far: YOLO-World-S, 14-word kitchen vocab, Apple Silicon MPS: **12 ms/frame (84 fps)** steady state.

## End-to-end: MP4 in, guided video out

The production demo path is:

```text
MP4 -> YOLO-World + MediaPipe hands + color/segmentation
    -> curated scene context -> Gemini VLM every 5 seconds
    -> seven-step SOP state engine -> overlays and coaching cues
    -> iFLYTEK streaming TTS -> annotated_narrated.mp4
```

One-time local setup:

```bash
brew install ffmpeg
mkdir -p weights
curl -L https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/latest/hand_landmarker.task \
  -o weights/hand_landmarker.task
```

Copy `.env.example` to `.env`, then fill `GEMINI_API_KEY`, `IFLYTEK_APP_ID`,
`IFLYTEK_API_KEY`, and `IFLYTEK_API_SECRET`. Real keys, model weights, source
videos, and generated sessions are intentionally excluded from Git.

Run the complete recognition and guidance path:

```bash
.venv/bin/python harness/run_pipeline.py \
  --source /absolute/path/to/cooking-demo.mp4 \
  --device mps \
  --vlm gemini \
  --vlm-interval 5 \
  --narrate iflytek \
  --language zh-CN \
  --iflytek-voice x4_yezi \
  --iflytek-speed 58 \
  --iflytek-volume 44 \
  --iflytek-pitch 46 \
  --run-tag full_demo_01
```

Use `--device cpu` on machines without Apple Metal. The default authorized
Chinese voice is iFLYTEK `x4_yezi` (小露), configurable through `.env` or
`--iflytek-voice`. Use a unique `--run-tag` for each run.

The command prints the run directory under `data/sessions/`. Its main output is
`annotated_narrated.mp4`; the same directory also contains `events.jsonl`,
`timeline.jsonl`, raw keyframes, `narration_schedule.json`, `report.md`, and
`meta.json` for replay and audit.

A successful full run prints `narrated -> .../annotated_narrated.mp4` and ends
with `final=completed`. Treat any `NARRATE ERROR` line as a failed voice-output
stage even though the visual analysis artifacts are intentionally preserved;
also confirm that `meta.json` reports `final_status: completed` and that the
narrated MP4 exists before presenting the result.

The latest full acceptance used `NC_AIV_FHF.mov`: 5,455 frames, 1,808 events,
seven of seven SOP transitions, final completion at 180.2 seconds, and a
verified 181.8-second 720p video with the iFLYTEK `x4_yezi` narration track.
