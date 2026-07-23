# NomaChef 诺妈

> **Mom, I cooked.** 「妈,我会做饭了。」

A chest-worn, real-time cooking copilot: a wide-angle camera watches your first-person view, voice guides you step by step, and a multi-signal perception stack verifies each step is actually done. Design principle: **verify outcome states, never recognize actions.**

Cooking is the wedge. The engine generalizes to any hands-on procedural task (accessibility, eldercare, lab protocols, assembly & repair, STEM education).

## Read first

**[CLAUDE.md](CLAUDE.md)** is the full project context: architecture, three-layer perception design, model choices, SOP schema, state engine, hardware, and hackathon priorities. All agents (Claude Code / Codex / OpenCode) and humans start there.

- Hardware shopping list: [docs/hardware-bom.md](docs/hardware-bom.md)

## Repo layout

```
perception/   Layer 1-3 perception (YOLO-World, MediaPipe, audio, VLM checks)
engine/       State engine: evidence-accumulation scoring, step progression
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
