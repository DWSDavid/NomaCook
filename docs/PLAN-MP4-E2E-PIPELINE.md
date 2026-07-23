# MP4 端到端管线实施计划 (总装 Plan)

> **执行者须知:** 本计划由 Claude(overall manager)编写,任务分配给 **Codex** 与 **OpenCode** 执行。
> 每个 Task 自包含:不需要读本仓库其它上下文即可动手。步骤用 `- [ ]` checkbox 跟踪,做完勾掉。
> 严格 TDD:先写失败测试,再写实现,每个 Task 一个 commit。
> 执行完任一 Task,在 `docs/PIPELINE-PROGRESS.md` 追加一行:`日期 | Task N | 执行者 | 结果 | commit hash`。

**Goal:** 一条命令把一个 MP4 做菜视频跑成:EventEnvelope 事件流 + 每 3 秒关键帧与 Timeline 状态对比 + StateEngine 步骤推进 + (可选)VLM 确认 + 标注后的分析 MP4 + 可读报告,且同一视频跑两遍事件流逐位一致。

**Architecture:** 复用已有且测试全绿的五个模块(perception 三件套、server/events 契约、server/engine 状态机、server/perception context 词表、server/vlm 契约),新增 `server/pipeline/` 总装层(确定性 ID/时钟、证据封装、关键帧 Timeline、渲染、VLM 钩子)和一个入口 `harness/run_pipeline.py`。所有新代码只做编排与适配,不改动既有模块行为(除计划中明确列出的 `perception/hands.py` 时间戳修正)。

**Tech Stack:** Python 3.12 (`.venv`),ultralytics YOLO-World,MediaPipe Tasks,OpenCV,pydantic,pytest。不新增任何第三方依赖(渲染中文可选用已随 ultralytics 安装的 Pillow,须带 ASCII 降级)。

## Global Constraints

- 一律用 `.venv/bin/python`;测试命令 `.venv/bin/python -m pytest tests/ -q`。
- 不 push、不 rebase、不 checkout、不 restore、不 stash;只 `git add <明确文件> && git commit`。
- commit message 前缀 `pipeline:`,结尾不加任何签名。
- 不新增依赖;不改 `server/events/`、`server/engine/`、`server/vlm/`、`server/perception/context.py` 的任何现有行为。
- **确定性契约(所有 Task 必须遵守):**
  - `t_device_ms = frame_idx * 1000.0 / fps`(用帧号推,不用 `CAP_PROP_POS_MSEC`,后者随编解码器抖动)。
  - `t_server_est = SESSION_EPOCH + t_device_ms`,`SESSION_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)`(固定常量,不用 `now()`)。
  - 离线管线中 `received_at = t_server_est`(等价于零传输延迟;engine 的 stale 判定因此恒为 0ms,这是有意的)。
  - `event_id = f"evt_{session_id}_{seq:08d}"`,`session_id` 由视频文件名 + recipe_version_id 派生,不含时间戳。
  - 回归/对比运行一律 `--device cpu`(MPS 不保证逐位一致)。
  - 同帧内事件发射顺序固定:interaction 事件(fusion 返回顺序)→ 到期的脚本事件(按 pts 再按文件内顺序)→ 关键帧时刻的 presence 事件(按 state 字典序)→ roi_color 事件。
- 产物全部落在 `data/sessions/<session_id>/run_<tag>/`(git 已忽略 data/sessions),做完可用 Task 2 的清理脚本删除。这个"每 3 秒一张关键帧 + 状态对比,本地暂存、用完即删"的形态就是将来 ESP32 端低频拍照上行的软件预演,采样间隔必须始终是 CLI 可调参数。

## 已有接口速查(执行者只需要这些,不用读源码全文)

```python
# perception/detector.py
ObjectDetector(vocab: list[str] | None = None, device: str = "mps", conf: float = 0.15)
  .set_vocab(vocab: list[str]) -> None
  .detect(frame_bgr: np.ndarray) -> list[Detection]   # Detection: .label .conf .box(xyxy int tuple)
  .last_latency_ms: float

# perception/hands.py  (Task 1 之后)
HandTracker().detect(frame_bgr, timestamp_ms: float | None = None) -> list[HandState]
  # HandState: .handedness "Left"/"Right", .landmarks_px, .box, .grip_closure, .is_gripping, .palm_center

# perception/fusion.py
InteractionTracker(k_frames=3).update(t: float, frame: int,
    hands: list[tuple[str, tuple[float,float], Box, bool]],
    detections: list[tuple[str, float, Box]]) -> list[InteractionEvent]
  # InteractionEvent: .t .frame .event("hand_holding_object"/"hand_near_object"/…"_end") .hand .object .conf .hand_box .obj_box

# server/events
create_event(*, session_id, seq, event_type, t_device_ms, t_server_est, source, payload,
             event_id=None, received_at=None, frame_id=None, backfill=False, confidence=None) -> EventEnvelope
EventLog(path).append(envelope) -> bool
# 对比 CLI: .venv/bin/python -m server.events.replay compare <left.jsonl> <right.jsonl>  → 一致则打印 "equal"

# server/engine
load_recipe(path) -> RecipeSOP           # .steps[i]: .id .instruction .objects_involved .completion_check .high_risk .completion_policy
StateEngine(session_id=..., recipe=..., started_at=<tz-aware datetime>)
  .context -> SessionContext             # .current_step_id .context_version .active_objects .step_progress.score .pending_question
  .current_step -> RecipeStep
  .consume(envelope) -> EngineResult     # .status in {duplicate, stale, unmatched, evidence_added, question_pending, step_completed, session_completed}
                                         # .transition(.decision_id .completed_step_id .next_step_id .score .decided_at) 或 None

# server/perception
build_detection_context(session_context, recipe) -> DetectionContext   # .prompts
ContextualVocabularyController(detector).sync(detection_context) -> bool
canonicalize_detections(raw_detections, detection_context) -> list[ContextDetection]  # .canonical_label .conf .box .role
extract_tomato_egg_color_signals(frame_bgr, roi_xyxy) -> TomatoEggColorSignals  # .state .confidence .payload(step_id)

# server/vlm
VLMDecisionRequest.create(*, decision_id, session_id, step_id, context_version, frame_id,
                          requested_at, completion_check, expected_objects=(), ttl_seconds=8)
GeminiVLMClient(api_key=None, model=None).analyze_image(request, image_bytes) -> VLMObservation
validate_observation(request, observation, received_at=<tz-aware>) -> ValidatedVLMResult
  .to_event(*, seq, t_device_ms, source) -> EventEnvelope
```

**SOP 证据事实(决定管线必须发什么事件):** `sop/tomato_egg.json` 每步阈值 0.7、连续达标 2 次,规则权重为 感知 0.2-0.3 / VLM 0.4 / 口头确认 0.3。**只靠感知信号永远推不完一步,这是设计而非 bug。** 因此管线必须支持 `--script`(脚本化注入 `vlm.step_assessment` 与 `voice.user_confirmation`,离线确定性跑通全流程)与 `--vlm gemini`(真实 VLM,声明为非确定性模式)两种补足方式。规则匹配的事件类型与 payload 关键字段:

| event_type | payload 必须字段 | 示例 |
|---|---|---|
| `perception.objects_present` | `step_id`, `state` | `state="tomato_egg_tools_ready"`(step_01), `state="food_on_plate"`(step_04) |
| `perception.roi_color` | `step_id`, `state` | `state="yellow_dominant"/"red_dominant"/"red_yellow_mixed"` |
| `vlm.step_assessment` | `step_id`, `phase` | `phase="likely_complete"`, confidence ≥ 0.75 |
| `voice.user_confirmation` | `step_id`, `confirmed=true`, `transcript_event_id` 非空;high_risk 步骤还需 `question_event_id` 非空 | confidence ≥ 0.9 |

## 分工与波次(避免同文件冲突的硬规则)

| 波次 | Codex | OpenCode | 完成后 |
|---|---|---|---|
| Wave 1 | Task 1 (`perception/hands.py`, `harness/live_perception.py`) | Task 2 (`server/pipeline/session.py`, `harness/clean_sessions.py`) | Claude review + 全量 pytest |
| Wave 2 | Task 3 (`server/pipeline/evidence.py`) | Task 4 (`server/pipeline/timeline.py`) | Claude review |
| Wave 3 | Task 5 (`harness/run_pipeline.py` 总装) | Task 6 (`server/pipeline/render.py`,只写模块不碰 runner) | Claude review + 确定性验收 |
| Wave 4 | 待命 | Task 7 (把 render 接进 runner + e2e 回归测试) | Claude review |
| Wave 5 | Task 8 (VLM 钩子接进 runner) | Task 9 (报告生成 + 旧 logger 废弃标注) | Claude 终验 |

规则:`harness/run_pipeline.py` 同一时间只属于一个 Task 的执行者;两个执行者永不并行修改同一文件。任何接口疑问,以本文件"已有接口速查"和各 Task 的 Interfaces 块为准,不自行发明。

---

### Task 1: 真实视频时间戳贯通 perception(Codex)

**Files:**
- Modify: `perception/hands.py`(`HandTracker.detect` 增加 `timestamp_ms` 参数)
- Modify: `harness/live_perception.py`(把帧时间传给 hands 与 fusion)
- Test: `tests/test_hands_timestamp.py`

**Interfaces:**
- Produces: `HandTracker.detect(frame_bgr, timestamp_ms: float | None = None)`。传入时用调用方时间(向上取整为严格递增的 int ms);不传时保持旧行为(内部 +33ms),不破坏现有调用。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_hands_timestamp.py
from __future__ import annotations

import numpy as np

from perception.hands import HandTracker


def _frame() -> np.ndarray:
    return np.zeros((120, 160, 3), dtype=np.uint8)


def test_external_timestamps_drive_internal_clock():
    tracker = HandTracker()
    try:
        tracker.detect(_frame(), timestamp_ms=0.0)
        tracker.detect(_frame(), timestamp_ms=33.4)
        tracker.detect(_frame(), timestamp_ms=66.7)
        assert tracker.last_timestamp_ms == 67  # ceil + 单调
    finally:
        tracker.close()


def test_non_increasing_timestamp_is_bumped_not_crashed():
    tracker = HandTracker()
    try:
        tracker.detect(_frame(), timestamp_ms=100.0)
        tracker.detect(_frame(), timestamp_ms=100.0)  # 同帧时间重复
        assert tracker.last_timestamp_ms == 101
    finally:
        tracker.close()


def test_default_still_uses_internal_33ms_clock():
    tracker = HandTracker()
    try:
        tracker.detect(_frame())
        tracker.detect(_frame())
        assert tracker.last_timestamp_ms == 66
    finally:
        tracker.close()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_hands_timestamp.py -v`
Expected: FAIL(`detect() got an unexpected keyword argument 'timestamp_ms'` 或无 `last_timestamp_ms` 属性)

- [ ] **Step 3: 实现**

`perception/hands.py` 中把 `detect` 改为(其余不动):

```python
    def detect(
        self, frame_bgr: np.ndarray, timestamp_ms: float | None = None
    ) -> list[HandState]:
        h, w = frame_bgr.shape[:2]
        rgb = np.ascontiguousarray(frame_bgr[:, :, ::-1])
        image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        if timestamp_ms is None:
            self._ts_ms += 33  # legacy webcam path: nominal 30 fps clock
        else:
            # Real frame time from the caller; Tasks API only needs strict
            # monotonic int milliseconds, so ceil and bump on collisions.
            candidate = int(math.ceil(timestamp_ms))
            self._ts_ms = max(candidate, self._ts_ms + 1)
        result = self._landmarker.detect_for_video(image, self._ts_ms)
```

文件顶部补 `import math`;类里加只读属性:

```python
    @property
    def last_timestamp_ms(self) -> int:
        return self._ts_ms
```

`harness/live_perception.py` 主循环改两处,让 webcam 路径也用统一时钟(视频回放时 `CAP_PROP_FPS` 有效):

```python
    fps_src = cap.get(cv2.CAP_PROP_FPS)
    frame_ms = 1000.0 / fps_src if fps_src and fps_src > 0 else None
```

循环内:

```python
            pts_ms = frame_idx * frame_ms if frame_ms else None
            hands = hand_tracker.detect(frame, timestamp_ms=pts_ms)
            events = fusion.update(
                t=(pts_ms / 1000.0) if pts_ms is not None else now,
                frame=frame_idx, ...
```

(`hands=...`/`detections=...` 两个实参保持原样。)

- [ ] **Step 4: 跑测试确认通过 + 全量回归**

Run: `.venv/bin/python -m pytest tests/test_hands_timestamp.py tests/ -q`
Expected: 新增 3 项 PASS,且 0 failed(总数随并行 Task 增长,不以绝对数字为准;manager 修正于 2026-07-23,原文误写 37 passed)

- [ ] **Step 5: Commit**

```bash
git add perception/hands.py harness/live_perception.py tests/test_hands_timestamp.py
git commit -m "pipeline: real frame timestamps through HandTracker and live harness"
```

---

### Task 2: 确定性 ID/时钟 + Session 产物目录 + 清理 CLI(OpenCode)

**Files:**
- Create: `server/pipeline/__init__.py`(空文件即可)
- Create: `server/pipeline/session.py`
- Create: `harness/clean_sessions.py`
- Test: `tests/test_pipeline_session.py`

**Interfaces:**
- Produces(后续所有 Task 依赖,签名不得偏离):
  - `SESSION_EPOCH: datetime`(2026-01-01 UTC 常量)
  - `session_id_for(video_path: str | Path, recipe_version_id: str) -> str`
  - `event_id_for(session_id: str, seq: int) -> str`
  - `t_server_for(pts_ms: float) -> datetime`
  - `SessionPaths`(dataclass,属性 `root/events/keyframes_dir/timeline/annotated/report/meta`)
  - `create_run_dir(session_id: str, base: Path = Path("data/sessions"), run_tag: str | None = None) -> SessionPaths`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_pipeline_session.py
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from server.pipeline.session import (
    SESSION_EPOCH,
    SessionPaths,
    create_run_dir,
    event_id_for,
    session_id_for,
    t_server_for,
)


def test_ids_and_clock_are_deterministic_and_filename_safe():
    sid = session_id_for("data/test_videos/My Video (1).mp4", "rv_tomato_egg_demo_1")
    assert sid == session_id_for("data/test_videos/My Video (1).mp4", "rv_tomato_egg_demo_1")
    assert sid == "ses_rv_tomato_egg_demo_1_my_video_1"
    assert event_id_for(sid, 7) == f"evt_{sid}_00000007"
    assert SESSION_EPOCH == datetime(2026, 1, 1, tzinfo=UTC)
    assert t_server_for(1500.0) == datetime(2026, 1, 1, 0, 0, 1, 500000, tzinfo=UTC)


def test_run_dir_layout(tmp_path: Path):
    paths = create_run_dir("ses_x", base=tmp_path, run_tag="a")
    assert paths.root == tmp_path / "ses_x" / "run_a"
    assert paths.keyframes_dir.is_dir()
    assert paths.events == paths.root / "events.jsonl"
    assert paths.timeline == paths.root / "timeline.jsonl"
    assert paths.annotated == paths.root / "annotated.mp4"
    assert paths.report == paths.root / "report.md"
    assert paths.meta == paths.root / "meta.json"


def test_same_run_tag_twice_raises(tmp_path: Path):
    create_run_dir("ses_x", base=tmp_path, run_tag="a")
    try:
        create_run_dir("ses_x", base=tmp_path, run_tag="a")
        raise AssertionError("expected FileExistsError")
    except FileExistsError:
        pass
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_pipeline_session.py -v`
Expected: FAIL(`ModuleNotFoundError: server.pipeline`)

- [ ] **Step 3: 实现 `server/pipeline/session.py`**

```python
"""Deterministic ids, offline clock, and per-run artifact layout."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
import re

SESSION_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def session_id_for(video_path: str | Path, recipe_version_id: str) -> str:
    return f"ses_{_slug(recipe_version_id)}_{_slug(Path(video_path).stem)}"


def event_id_for(session_id: str, seq: int) -> str:
    return f"evt_{session_id}_{seq:08d}"


def t_server_for(pts_ms: float) -> datetime:
    return SESSION_EPOCH + timedelta(milliseconds=pts_ms)


@dataclass(frozen=True)
class SessionPaths:
    root: Path

    @property
    def events(self) -> Path:
        return self.root / "events.jsonl"

    @property
    def keyframes_dir(self) -> Path:
        return self.root / "keyframes"

    @property
    def timeline(self) -> Path:
        return self.root / "timeline.jsonl"

    @property
    def annotated(self) -> Path:
        return self.root / "annotated.mp4"

    @property
    def report(self) -> Path:
        return self.root / "report.md"

    @property
    def meta(self) -> Path:
        return self.root / "meta.json"


def create_run_dir(
    session_id: str,
    base: Path = Path("data/sessions"),
    run_tag: str | None = None,
) -> SessionPaths:
    tag = run_tag or datetime.now().strftime("%Y%m%dT%H%M%S")
    root = base / session_id / f"run_{tag}"
    root.mkdir(parents=True, exist_ok=False)
    (root / "keyframes").mkdir()
    return SessionPaths(root=root)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_pipeline_session.py -v`
Expected: 3 PASS

- [ ] **Step 5: 写清理 CLI `harness/clean_sessions.py`(做完菜即删的入口)**

```python
"""Delete pipeline run artifacts under data/sessions (keyframes are transient).

Usage:
    .venv/bin/python harness/clean_sessions.py --dry-run
    .venv/bin/python harness/clean_sessions.py --keep 1     # keep newest run per session
    .venv/bin/python harness/clean_sessions.py --all
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_BASE = Path(__file__).resolve().parent.parent / "data" / "sessions"


def run_dirs(base: Path) -> dict[Path, list[Path]]:
    sessions: dict[Path, list[Path]] = {}
    if not base.is_dir():
        return sessions
    for session_dir in sorted(base.glob("ses_*")):
        runs = sorted(d for d in session_dir.glob("run_*") if d.is_dir())
        if runs:
            sessions[session_dir] = runs
    return sessions


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", type=Path, default=DEFAULT_BASE)
    ap.add_argument("--keep", type=int, default=0, help="newest runs to keep per session")
    ap.add_argument("--all", action="store_true", help="required to delete with --keep 0")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.keep == 0 and not args.all and not args.dry_run:
        ap.error("refusing to delete everything without --all (or use --dry-run)")

    doomed: list[Path] = []
    for _session_dir, runs in run_dirs(args.base).items():
        keep = args.keep if args.keep > 0 else 0
        doomed.extend(runs[: len(runs) - keep] if keep else runs)

    for path in doomed:
        print(("DRY-RUN would delete: " if args.dry_run else "deleting: ") + str(path))
        if not args.dry_run:
            shutil.rmtree(path)
    print(f"{len(doomed)} run dir(s) {'listed' if args.dry_run else 'deleted'}")


if __name__ == "__main__":
    main()
```

手动验收:`.venv/bin/python harness/clean_sessions.py --dry-run` 输出 0 个(还没有 run 目录),不报错。

- [ ] **Step 6: 全量回归 + Commit**

Run: `.venv/bin/python -m pytest tests/ -q` → 全绿

```bash
git add server/pipeline/__init__.py server/pipeline/session.py harness/clean_sessions.py tests/test_pipeline_session.py
git commit -m "pipeline: deterministic session ids, run dir layout, cleanup CLI"
```

---

### Task 3: 证据封装层(感知输出 → EventEnvelope)(Codex)

**Files:**
- Create: `server/pipeline/evidence.py`
- Test: `tests/test_pipeline_evidence.py`

**Interfaces:**
- Consumes: Task 2 的 `event_id_for` / `t_server_for`;`perception.fusion.InteractionEvent`;`server.perception.ContextDetection`;`server.perception.tomato_egg_signals.TomatoEggColorSignals`。
- Produces(Task 5 依赖):
  - `interaction_event(ev: InteractionEvent, *, session_id: str, seq: int) -> EventEnvelope`(`ev.t` 单位是秒,内部乘 1000 得 pts_ms;type `perception.hand_object_relation`)
  - `presence_states(step_id: str, detections: Sequence) -> list[tuple[str, float]]`(detections 元素需有 `.canonical_label` 和 `.conf`)
  - `objects_present_event(state: str, conf: float, *, session_id, seq, step_id, pts_ms, frame_idx) -> EventEnvelope`
  - `roi_color_event(signals, *, session_id, seq, step_id, pts_ms, frame_idx) -> EventEnvelope`
  - `load_script(path) -> list[dict]` 与 `scripted_event(row: dict, index: int, *, session_id, seq, question_event_id: str | None) -> EventEnvelope`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_pipeline_evidence.py
from __future__ import annotations

from dataclasses import dataclass

from perception.fusion import InteractionEvent
from server.pipeline.evidence import (
    interaction_event,
    objects_present_event,
    presence_states,
    scripted_event,
)


@dataclass(frozen=True)
class FakeDet:
    canonical_label: str
    conf: float


def _interaction() -> InteractionEvent:
    return InteractionEvent(
        t=1.5, frame=45, event="hand_holding_object", hand="Right",
        object="bowl", conf=0.62, hand_box=(0, 0, 10, 10), obj_box=(2, 2, 12, 12),
    )


def test_interaction_event_is_deterministic_and_valid():
    a = interaction_event(_interaction(), session_id="ses_x", seq=3)
    b = interaction_event(_interaction(), session_id="ses_x", seq=3)
    assert a.canonical_dict() == b.canonical_dict()
    assert a.event_id == "evt_ses_x_00000003"
    assert a.type == "perception.hand_object_relation"
    assert a.t_device_ms == 1500.0
    assert a.payload["relation"] == "holding"
    assert a.payload["phase"] == "start"
    assert a.payload["hand"] == "right"
    assert a.payload["object_class"] == "bowl"


def test_end_event_maps_to_phase_end():
    ev = _interaction()
    end = InteractionEvent(**{**ev.__dict__, "event": "hand_holding_object_end"})
    env = interaction_event(end, session_id="ses_x", seq=4)
    assert env.payload["phase"] == "end"


def test_presence_states_require_all_objects_and_use_min_conf():
    dets = [FakeDet("tomato", 0.8), FakeDet("egg", 0.7), FakeDet("bowl", 0.66)]
    assert presence_states("step_01_prepare", dets) == [
        ("tomato_egg_tools_ready", 0.66)
    ]
    assert presence_states("step_01_prepare", dets[:2]) == []
    assert presence_states("step_02_scramble_egg", dets) == []


def test_objects_present_event_matches_sop_payload_contract():
    env = objects_present_event(
        "tomato_egg_tools_ready", 0.66, session_id="ses_x", seq=5,
        step_id="step_01_prepare", pts_ms=3000.0, frame_idx=90,
    )
    assert env.type == "perception.objects_present"
    assert env.payload == {"step_id": "step_01_prepare", "state": "tomato_egg_tools_ready"}
    assert env.confidence == 0.66
    assert env.frame_id == "frame_000090"


def test_scripted_vlm_and_confirmation_events():
    vlm = scripted_event(
        {"pts_ms": 400, "type": "vlm.step_assessment",
         "step_id": "step_01_prepare", "phase": "likely_complete", "confidence": 0.85},
        index=0, session_id="ses_x", seq=6, question_event_id=None,
    )
    assert vlm.type == "vlm.step_assessment"
    assert vlm.payload["phase"] == "likely_complete"
    assert vlm.confidence == 0.85

    ok = scripted_event(
        {"pts_ms": 600, "type": "voice.user_confirmation", "step_id": "step_02_scramble_egg"},
        index=1, session_id="ses_x", seq=7, question_event_id="evt_ses_x_00000002",
    )
    assert ok.payload["confirmed"] is True
    assert ok.payload["transcript_event_id"] == "script_line_1"
    assert ok.payload["question_event_id"] == "evt_ses_x_00000002"
    assert ok.confidence == 0.95
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_pipeline_evidence.py -v`
Expected: FAIL(`ModuleNotFoundError` 或函数缺失)

- [ ] **Step 3: 实现 `server/pipeline/evidence.py`**

```python
"""Wrap perception / scripted signals into deterministic EventEnvelopes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

from perception.fusion import InteractionEvent
from server.events import EventEnvelope, create_event
from server.events.schema import EvidencePayload
from server.perception.tomato_egg_signals import TomatoEggColorSignals

from .session import event_id_for, t_server_for

INTERACTION_TYPE = "perception.hand_object_relation"

# Demo presence rules keyed by tomato-egg SOP step ids. Each entry:
# (payload state string expected by the SOP, canonical labels that must all
# be visible in the same keyframe). Confidence is the weakest member.
TOMATO_EGG_PRESENCE: dict[str, list[tuple[str, frozenset[str]]]] = {
    "step_01_prepare": [("tomato_egg_tools_ready", frozenset({"tomato", "egg", "bowl"}))],
    "step_04_combine_and_plate": [("food_on_plate", frozenset({"plate", "wok"}))],
}


def _base(*, session_id: str, seq: int, event_type: str, pts_ms: float,
          frame_idx: int | None, source: str, payload: Any,
          confidence: float | None) -> EventEnvelope:
    stamp = t_server_for(pts_ms)
    return create_event(
        session_id=session_id,
        seq=seq,
        event_type=event_type,
        t_device_ms=pts_ms,
        t_server_est=stamp,
        received_at=stamp,  # offline: zero transport delay, fully deterministic
        frame_id=None if frame_idx is None else f"frame_{frame_idx:06d}",
        source=source,
        payload=payload,
        event_id=event_id_for(session_id, seq),
        confidence=confidence,
    )


def interaction_event(ev: InteractionEvent, *, session_id: str, seq: int) -> EventEnvelope:
    relation = "holding" if "holding" in ev.event else "near"
    hand = ev.hand.lower() if ev.hand.lower() in ("left", "right") else "unknown"
    payload = EvidencePayload(
        relation=relation,
        phase="end" if ev.event.endswith("_end") else "start",
        hand=hand,
        object_class=ev.object,
        relation_confidence=ev.conf,
        signals={},
    )
    return _base(
        session_id=session_id, seq=seq, event_type=INTERACTION_TYPE,
        pts_ms=ev.t * 1000.0, frame_idx=ev.frame, source="fusion_v1",
        payload=payload, confidence=ev.conf,
    )


def presence_states(step_id: str, detections: Sequence[Any]) -> list[tuple[str, float]]:
    best: dict[str, float] = {}
    for det in detections:
        label = det.canonical_label
        best[label] = max(best.get(label, 0.0), float(det.conf))
    states: list[tuple[str, float]] = []
    for state, required in TOMATO_EGG_PRESENCE.get(step_id, []):
        if required <= set(best):
            states.append((state, round(min(best[label] for label in required), 4)))
    return sorted(states)


def objects_present_event(state: str, conf: float, *, session_id: str, seq: int,
                          step_id: str, pts_ms: float, frame_idx: int) -> EventEnvelope:
    return _base(
        session_id=session_id, seq=seq, event_type="perception.objects_present",
        pts_ms=pts_ms, frame_idx=frame_idx, source="context_presence_v1",
        payload={"step_id": step_id, "state": state}, confidence=conf,
    )


def roi_color_event(signals: TomatoEggColorSignals, *, session_id: str, seq: int,
                    step_id: str, pts_ms: float, frame_idx: int) -> EventEnvelope:
    return _base(
        session_id=session_id, seq=seq, event_type="perception.roi_color",
        pts_ms=pts_ms, frame_idx=frame_idx, source="opencv_hsv_tomato_egg_v1",
        payload=signals.payload(step_id), confidence=signals.confidence,
    )


def load_script(path: str | Path) -> list[dict]:
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError("script must be a JSON array")
    return sorted(enumerate(rows), key=lambda item: (item[1]["pts_ms"], item[0]))  # type: ignore[return-value]


def scripted_event(row: dict, index: int, *, session_id: str, seq: int,
                   question_event_id: str | None) -> EventEnvelope:
    pts_ms = float(row["pts_ms"])
    step_id = row["step_id"]
    if row["type"] == "vlm.step_assessment":
        return _base(
            session_id=session_id, seq=seq, event_type="vlm.step_assessment",
            pts_ms=pts_ms, frame_idx=None, source="scripted_vlm_v0",
            payload={"step_id": step_id, "phase": row.get("phase", "likely_complete"),
                     "reason": "scripted"},
            confidence=float(row.get("confidence", 0.8)),
        )
    if row["type"] == "voice.user_confirmation":
        return _base(
            session_id=session_id, seq=seq, event_type="voice.user_confirmation",
            pts_ms=pts_ms, frame_idx=None, source="scripted_voice_v0",
            payload={"step_id": step_id, "confirmed": True,
                     "transcript_event_id": f"script_line_{index}",
                     "question_event_id": question_event_id or f"script_q_{index}"},
            confidence=float(row.get("confidence", 0.95)),
        )
    raise ValueError(f"unsupported scripted event type {row['type']!r}")
```

**已裁定(Wave 2)**:`load_script` 返回 `list[dict]`,每行注入 `"_index"`(原始行号),按 `pts_ms` 再按 `_index` 稳定排序;Task 5 的消费代码已同步为此形式。

- [ ] **Step 4: 跑测试确认通过 + 引擎联动冒烟**

Run: `.venv/bin/python -m pytest tests/test_pipeline_evidence.py tests/ -q`
Expected: 全绿。另跑一个一次性联动检查(不入库,终端执行):

```bash
.venv/bin/python - <<'EOF'
from server.engine import StateEngine, load_recipe
from server.pipeline.evidence import scripted_event
from server.pipeline.session import SESSION_EPOCH
recipe = load_recipe("sop/tomato_egg.json")
eng = StateEngine(session_id="ses_smoke", recipe=recipe, started_at=SESSION_EPOCH)
r1 = eng.consume(scripted_event({"pts_ms": 100, "type": "vlm.step_assessment", "step_id": "step_01_prepare", "phase": "likely_complete", "confidence": 0.85}, 0, session_id="ses_smoke", seq=0, question_event_id=None))
r2 = eng.consume(scripted_event({"pts_ms": 200, "type": "voice.user_confirmation", "step_id": "step_01_prepare"}, 1, session_id="ses_smoke", seq=1, question_event_id=None))
r3 = eng.consume(scripted_event({"pts_ms": 300, "type": "vlm.step_assessment", "step_id": "step_01_prepare", "phase": "likely_complete", "confidence": 0.85}, 2, session_id="ses_smoke", seq=2, question_event_id=None))
print(r1.status, r2.status, r3.status)  # 期望: evidence_added evidence_added step_completed
EOF
```

Expected 输出:`evidence_added evidence_added step_completed`。若不是,不要改 engine,回头检查 payload 字段。

- [ ] **Step 5: Commit**

```bash
git add server/pipeline/evidence.py tests/test_pipeline_evidence.py
git commit -m "pipeline: evidence adapters from perception and scripted signals to envelopes"
```

---

### Task 4: 关键帧采样 + Timeline 状态对比(OpenCode)

**Files:**
- Create: `server/pipeline/timeline.py`
- Test: `tests/test_pipeline_timeline.py`

**Interfaces:**
- Produces(Task 5/9 依赖):
  - `StateSnapshot`(frozen dataclass:`pts_ms: float, frame_idx: int, step_id: str, context_version: int, score: float, pending_question: str | None, detections: tuple[tuple[str, float], ...], color_state: str | None`)
  - `KeyframeSampler(interval_ms: float).due(pts_ms: float) -> bool`(首帧即触发,之后每 interval 一次)
  - `diff_snapshots(prev: StateSnapshot | None, cur: StateSnapshot) -> dict`
  - `keyframe_row(cur: StateSnapshot, diff: dict, jpg_name: str) -> dict`
  - `append_jsonl(path: Path, row: dict) -> None`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_pipeline_timeline.py
from __future__ import annotations

import json
from pathlib import Path

from server.pipeline.timeline import (
    KeyframeSampler,
    StateSnapshot,
    append_jsonl,
    diff_snapshots,
    keyframe_row,
)


def _snap(pts: float, **overrides) -> StateSnapshot:
    base = dict(
        pts_ms=pts, frame_idx=int(pts // 33), step_id="step_01_prepare",
        context_version=1, score=0.0, pending_question=None,
        detections=(("bowl", 0.7), ("egg", 0.5)), color_state=None,
    )
    base.update(overrides)
    return StateSnapshot(**base)


def test_sampler_fires_on_first_frame_then_every_interval():
    sampler = KeyframeSampler(interval_ms=3000.0)
    fired = [pts for pts in (0.0, 1000.0, 2999.0, 3000.0, 5900.0, 6000.0, 9100.0)
             if sampler.due(pts)]
    assert fired == [0.0, 3000.0, 6000.0, 9100.0]


def test_diff_reports_step_score_objects_and_color_changes():
    prev = _snap(0.0, score=0.3, color_state="uncertain")
    cur = _snap(
        3000.0, step_id="step_02_scramble_egg", context_version=5, score=0.0,
        detections=(("wok", 0.8), ("egg", 0.5)), color_state="yellow_dominant",
    )
    diff = diff_snapshots(prev, cur)
    assert diff["step_changed"] is True
    assert diff["score_delta"] == -0.3
    assert diff["objects_appeared"] == ["wok"]
    assert diff["objects_gone"] == ["bowl"]
    assert diff["color_changed"] is True


def test_first_keyframe_diff_is_empty_baseline():
    diff = diff_snapshots(None, _snap(0.0))
    assert diff == {"baseline": True}


def test_keyframe_row_roundtrips_through_jsonl(tmp_path: Path):
    row = keyframe_row(_snap(3000.0, score=0.4), {"baseline": True}, "kf_000090_3000ms.jpg")
    path = tmp_path / "timeline.jsonl"
    append_jsonl(path, row)
    append_jsonl(path, row)
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(lines) == 2
    assert lines[0]["jpg"] == "kf_000090_3000ms.jpg"
    assert lines[0]["step_id"] == "step_01_prepare"
    assert lines[0]["score"] == 0.4
    assert lines[0]["diff"] == {"baseline": True}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_pipeline_timeline.py -v`
Expected: FAIL(模块不存在)

- [ ] **Step 3: 实现 `server/pipeline/timeline.py`**

```python
"""Periodic keyframe snapshots and timeline diffs (the 3-5s state ledger)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class StateSnapshot:
    pts_ms: float
    frame_idx: int
    step_id: str
    context_version: int
    score: float
    pending_question: str | None
    detections: tuple[tuple[str, float], ...]
    color_state: str | None


class KeyframeSampler:
    """Fire on the first frame, then once per interval of video time."""

    def __init__(self, interval_ms: float) -> None:
        if interval_ms <= 0:
            raise ValueError("interval_ms must be positive")
        self.interval_ms = interval_ms
        self._next_at = 0.0

    def due(self, pts_ms: float) -> bool:
        if pts_ms < self._next_at:
            return False
        self._next_at = pts_ms + self.interval_ms
        return True


def diff_snapshots(prev: StateSnapshot | None, cur: StateSnapshot) -> dict:
    if prev is None:
        return {"baseline": True}
    prev_objs = {label for label, _conf in prev.detections}
    cur_objs = {label for label, _conf in cur.detections}
    return {
        "step_changed": cur.step_id != prev.step_id,
        "score_delta": round(cur.score - prev.score, 4),
        "objects_appeared": sorted(cur_objs - prev_objs),
        "objects_gone": sorted(prev_objs - cur_objs),
        "color_changed": cur.color_state != prev.color_state,
    }


def keyframe_row(cur: StateSnapshot, diff: dict, jpg_name: str) -> dict:
    row = asdict(cur)
    row["detections"] = [list(item) for item in cur.detections]
    row["jpg"] = jpg_name
    row["diff"] = diff
    return row


def append_jsonl(path: Path, row: dict) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_pipeline_timeline.py tests/ -q`
Expected: 全绿

- [ ] **Step 5: Commit**

```bash
git add server/pipeline/timeline.py tests/test_pipeline_timeline.py
git commit -m "pipeline: keyframe sampler and timeline state diffs"
```

---

### Task 5: 总装 runner `harness/run_pipeline.py`(Codex,本计划核心)

**Files:**
- Create: `harness/run_pipeline.py`
- Create: `tests/fixtures/tomato_egg_full_script.json`(脚本化证据,离线推完 4 步)
- Test: 本 Task 用命令行验收(自动化 e2e 在 Task 7)

**Interfaces:**
- Consumes: Task 1-4 的全部 Produces + "已有接口速查"。
- Produces: CLI 契约(Task 7/8/9 依赖,参数名不得改):
  `--source --sop --device --detect-every --keyframe-interval --script --run-tag --max-frames --k-frames`
  产物:`events.jsonl`(EventLog 格式)、`timeline.jsonl`、`keyframes/kf_<frame:06d>_<pts:int>ms.jpg`、`meta.json`。
  以及函数 `run(args) -> dict`(返回 meta dict,`main()` 只做 argparse + `run`,方便测试进程内调用)。

- [ ] **Step 1: 写脚本 fixture `tests/fixtures/tomato_egg_full_script.json`**

每步 3 个事件(vlm → confirm → vlm)推进,4 步共 12 行,pts 间隔 200ms 落在任何 ≥3 秒的视频里:

```json
[
  {"pts_ms": 200,  "type": "vlm.step_assessment",     "step_id": "step_01_prepare",           "phase": "likely_complete", "confidence": 0.85},
  {"pts_ms": 400,  "type": "voice.user_confirmation", "step_id": "step_01_prepare"},
  {"pts_ms": 600,  "type": "vlm.step_assessment",     "step_id": "step_01_prepare",           "phase": "likely_complete", "confidence": 0.85},
  {"pts_ms": 800,  "type": "vlm.step_assessment",     "step_id": "step_02_scramble_egg",      "phase": "likely_complete", "confidence": 0.85},
  {"pts_ms": 1000, "type": "voice.user_confirmation", "step_id": "step_02_scramble_egg"},
  {"pts_ms": 1200, "type": "vlm.step_assessment",     "step_id": "step_02_scramble_egg",      "phase": "likely_complete", "confidence": 0.85},
  {"pts_ms": 1400, "type": "vlm.step_assessment",     "step_id": "step_03_soften_tomato",     "phase": "likely_complete", "confidence": 0.85},
  {"pts_ms": 1600, "type": "voice.user_confirmation", "step_id": "step_03_soften_tomato"},
  {"pts_ms": 1800, "type": "vlm.step_assessment",     "step_id": "step_03_soften_tomato",     "phase": "likely_complete", "confidence": 0.85},
  {"pts_ms": 2000, "type": "vlm.step_assessment",     "step_id": "step_04_combine_and_plate", "phase": "likely_complete", "confidence": 0.85},
  {"pts_ms": 2200, "type": "voice.user_confirmation", "step_id": "step_04_combine_and_plate"},
  {"pts_ms": 2400, "type": "vlm.step_assessment",     "step_id": "step_04_combine_and_plate", "phase": "likely_complete", "confidence": 0.85}
]
```

- [ ] **Step 2: 实现 `harness/run_pipeline.py`**

```python
"""Assemble the full offline pipeline: MP4 -> perception -> envelopes ->
EventLog -> StateEngine -> keyframes/timeline (-> render/VLM added later).

Usage:
    .venv/bin/python harness/run_pipeline.py --source data/test_videos/x.mp4
    .venv/bin/python harness/run_pipeline.py --source x.mp4 --device cpu \
        --script tests/fixtures/tomato_egg_full_script.json --run-tag a
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from perception.detector import ObjectDetector
from perception.fusion import InteractionTracker
from perception.hands import HandTracker
from server.engine import StateEngine, load_recipe
from server.events.log import EventLog
from server.perception import (
    ContextualVocabularyController,
    build_detection_context,
    canonicalize_detections,
    extract_tomato_egg_color_signals,
)
from server.pipeline.evidence import (
    interaction_event,
    load_script,
    objects_present_event,
    presence_states,
    roi_color_event,
    scripted_event,
)
from server.pipeline.session import (
    SESSION_EPOCH,
    create_run_dir,
    session_id_for,
)
from server.pipeline.timeline import (
    KeyframeSampler,
    StateSnapshot,
    append_jsonl,
    diff_snapshots,
    keyframe_row,
)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", required=True, help="video file path (not webcam)")
    ap.add_argument("--sop", default="sop/tomato_egg.json")
    ap.add_argument("--device", default="cpu", help="yolo device; cpu for determinism")
    ap.add_argument("--detect-every", type=int, default=3)
    ap.add_argument("--keyframe-interval", type=float, default=3.0, help="seconds")
    ap.add_argument("--script", default=None, help="scripted evidence JSON")
    ap.add_argument("--run-tag", default=None)
    ap.add_argument("--max-frames", type=int, default=0)
    ap.add_argument("--k-frames", type=int, default=3, help="fusion debounce")
    return ap


def run(args: argparse.Namespace) -> dict:
    video = Path(args.source)
    if not video.is_file():
        raise SystemExit(f"--source must be an existing video file: {video}")
    recipe = load_recipe(args.sop)
    session_id = session_id_for(video, recipe.recipe_version_id)
    paths = create_run_dir(session_id, run_tag=args.run_tag)
    print(f"session={session_id}\nrun dir={paths.root}")

    capture = cv2.VideoCapture(str(video))
    if not capture.isOpened():
        raise SystemExit(f"cannot open video {video}")
    fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
    frame_ms = 1000.0 / fps

    engine = StateEngine(session_id=session_id, recipe=recipe, started_at=SESSION_EPOCH)
    log = EventLog(paths.events)
    detector = ObjectDetector(device=args.device, conf=0.10)
    controller = ContextualVocabularyController(detector)
    det_ctx = build_detection_context(engine.context, recipe)
    controller.sync(det_ctx)
    hand_tracker = HandTracker()
    fusion = InteractionTracker(k_frames=args.k_frames)
    sampler = KeyframeSampler(interval_ms=args.keyframe_interval * 1000.0)
    script = load_script(args.script) if args.script else []
    script_cursor = 0

    seq = 0
    frame_idx = 0
    latest_canon = []
    color_state: str | None = None
    prev_snapshot: StateSnapshot | None = None
    transitions: list[dict] = []
    wall_start = time.perf_counter()

    def emit(envelope) -> None:
        nonlocal seq, det_ctx
        log.append(envelope)
        result = engine.consume(envelope)
        seq += 1
        if result.transition is not None:
            transitions.append({
                "decision_id": result.transition.decision_id,
                "completed_step_id": result.transition.completed_step_id,
                "next_step_id": result.transition.next_step_id,
                "score": result.transition.score,
                "pts_ms": envelope.t_device_ms,
            })
            print(f"[{envelope.t_device_ms:8.0f}ms] STEP DONE "
                  f"{result.transition.completed_step_id} -> "
                  f"{result.transition.next_step_id or 'SESSION COMPLETE'}")
            if result.transition.next_step_id is not None:
                det_ctx = build_detection_context(engine.context, recipe)
                controller.sync(det_ctx)
        elif result.status == "question_pending" and engine.context.pending_question:
            print(f"[{envelope.t_device_ms:8.0f}ms] QUESTION: "
                  f"{engine.context.pending_question.question}")

    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            pts_ms = frame_idx * frame_ms
            step_id = engine.context.current_step_id

            if frame_idx % args.detect_every == 0:
                raw = detector.detect(frame)
                latest_canon = canonicalize_detections(raw, det_ctx)
            hands = hand_tracker.detect(frame, timestamp_ms=pts_ms)

            for ev in fusion.update(
                t=pts_ms / 1000.0, frame=frame_idx,
                hands=[(h.handedness, h.palm_center, h.box, h.is_gripping)
                       for h in hands],
                detections=[(d.canonical_label, d.conf, d.box)
                            for d in latest_canon],
            ):
                emit(interaction_event(ev, session_id=session_id, seq=seq))

            while script_cursor < len(script) and script[script_cursor]["pts_ms"] <= pts_ms:
                row = script[script_cursor]
                pending = engine.context.pending_question
                question_ref = (pending.triggered_by_event_id if pending else None)
                emit(scripted_event(row, row["_index"], session_id=session_id,
                                    seq=seq, question_event_id=question_ref))
                script_cursor += 1

            if sampler.due(pts_ms):
                for state, conf in presence_states(step_id, latest_canon):
                    emit(objects_present_event(
                        state, conf, session_id=session_id, seq=seq,
                        step_id=step_id, pts_ms=pts_ms, frame_idx=frame_idx))
                wok = next((d for d in latest_canon
                            if d.canonical_label == "wok" and d.role == "primary"), None)
                if wok is not None:
                    signals = extract_tomato_egg_color_signals(frame, wok.box)
                    color_state = signals.state
                    emit(roi_color_event(signals, session_id=session_id, seq=seq,
                                         step_id=step_id, pts_ms=pts_ms,
                                         frame_idx=frame_idx))
                snapshot = StateSnapshot(
                    pts_ms=pts_ms, frame_idx=frame_idx,
                    step_id=engine.context.current_step_id,
                    context_version=engine.context.context_version,
                    score=engine.context.step_progress.score,
                    pending_question=(engine.context.pending_question.question
                                      if engine.context.pending_question else None),
                    detections=tuple(sorted((d.canonical_label, round(d.conf, 2))
                                            for d in latest_canon)),
                    color_state=color_state,
                )
                jpg_name = f"kf_{frame_idx:06d}_{int(pts_ms)}ms.jpg"
                cv2.imwrite(str(paths.keyframes_dir / jpg_name), frame)
                append_jsonl(paths.timeline,
                             keyframe_row(snapshot, diff_snapshots(prev_snapshot, snapshot),
                                          jpg_name))
                prev_snapshot = snapshot

            frame_idx += 1
            if args.max_frames and frame_idx >= args.max_frames:
                break
    finally:
        capture.release()
        hand_tracker.close()

    meta = {
        "session_id": session_id,
        "video": str(video),
        "sop": args.sop,
        "fps": fps,
        "frames": frame_idx,
        "events": len(log),
        "transitions": transitions,
        "final_step_id": engine.context.current_step_id,
        "final_status": engine.context.step_status,
        "wall_seconds": round(time.perf_counter() - wall_start, 2),
        "args": {k: v for k, v in vars(args).items()},
    }
    paths.meta.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    print(f"frames={frame_idx} events={len(log)} "
          f"transitions={len(transitions)} final={meta['final_status']}")
    return meta


def main() -> None:
    run(build_parser().parse_args())


if __name__ == "__main__":
    main()
```

注意:`engine.context.step_status` 若属性名不同(见 `server/engine/models.py`),以 models 实际字段为准调整,不得改 models。

- [ ] **Step 3: 手动验收 A(能跑 + 推完 4 步)**

```bash
.venv/bin/python harness/run_pipeline.py --source data/test_videos/synthetic_smoke.mp4 --device cpu --script tests/fixtures/tomato_egg_full_script.json --run-tag manual_a --max-frames 120
```

Expected:打印 4 次 `STEP DONE`,最后一行 `final=completed`;run 目录下有 `events.jsonl`、`timeline.jsonl`、若干 `keyframes/*.jpg`、`meta.json`。

- [ ] **Step 4: 手动验收 B(确定性,本计划最重要的一条验收)**

```bash
.venv/bin/python harness/run_pipeline.py --source data/test_videos/synthetic_smoke.mp4 --device cpu --script tests/fixtures/tomato_egg_full_script.json --run-tag det_1 --max-frames 120
.venv/bin/python harness/run_pipeline.py --source data/test_videos/synthetic_smoke.mp4 --device cpu --script tests/fixtures/tomato_egg_full_script.json --run-tag det_2 --max-frames 120
SID=$(ls data/sessions | grep synthetic_smoke | head -1)
.venv/bin/python -m server.events.replay compare data/sessions/$SID/run_det_1/events.jsonl data/sessions/$SID/run_det_2/events.jsonl
```

Expected:最后一条命令输出 `equal`。若不 equal,打印的第一处 diff 会指出哪个字段不确定,常见嫌疑:忘了传 `received_at`、用了 `now()`、YOLO 跑在 mps。修到 equal 为止。

- [ ] **Step 5: 全量回归 + Commit**

Run: `.venv/bin/python -m pytest tests/ -q` → 全绿

```bash
git add harness/run_pipeline.py tests/fixtures/tomato_egg_full_script.json
git commit -m "pipeline: end-to-end runner from mp4 to events, engine, keyframes, timeline"
```

---

### Task 6: 标注视频渲染模块(OpenCode,只写模块,不碰 runner)

**Files:**
- Create: `server/pipeline/render.py`
- Test: `tests/test_pipeline_render.py`

**Interfaces:**
- Produces(Task 7 依赖):
  - `AnnotatedVideoWriter(path: Path, fps: float, frame_size: tuple[int, int])`,方法 `.write(frame_bgr) -> None`、`.close() -> None`、属性 `.frames_written: int`
  - `draw_overlay(frame_bgr, *, detections: Sequence, step_id: str, instruction: str, score: float, threshold: float, pending_question: str | None, recent_events: Sequence[str], color_text: str | None) -> None`(原地画,detections 元素有 `.canonical_label .conf .box .role`)
- 中文渲染:优先用 Pillow + `/System/Library/Fonts/PingFang.ttc` 画顶栏;import 失败或字体不存在时降级为 `cv2.putText` 只画 ASCII(step_id + score),不许崩。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_pipeline_render.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from server.pipeline.render import AnnotatedVideoWriter, draw_overlay


@dataclass(frozen=True)
class FakeDet:
    canonical_label: str
    conf: float
    box: tuple[int, int, int, int]
    role: str


def test_draw_overlay_mutates_frame_without_crashing_on_chinese():
    frame = np.zeros((240, 320, 3), dtype=np.uint8)
    before = frame.copy()
    draw_overlay(
        frame,
        detections=[FakeDet("wok", 0.83, (10, 40, 200, 200), "primary")],
        step_id="step_02_scramble_egg",
        instruction="热锅加油，倒入蛋液，翻炒至凝固。",
        score=0.4, threshold=0.7,
        pending_question="鸡蛋已经凝固成块并盛出来了吗？",
        recent_events=["hand_holding_object Right/bowl"],
        color_text="color=yellow_dominant",
    )
    assert (frame != before).any()


def test_writer_writes_frames_and_reports_count(tmp_path: Path):
    out = tmp_path / "annotated.mp4"
    writer = AnnotatedVideoWriter(out, fps=30.0, frame_size=(320, 240))
    for _ in range(9):
        writer.write(np.zeros((240, 320, 3), dtype=np.uint8))
    writer.close()
    assert writer.frames_written == 9
    probe = cv2.VideoCapture(str(out))
    assert int(probe.get(cv2.CAP_PROP_FRAME_COUNT)) == 9
    probe.release()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_pipeline_render.py -v` → FAIL(模块不存在)

- [ ] **Step 3: 实现 `server/pipeline/render.py`**

```python
"""Overlay pipeline state onto frames and write the annotated MP4."""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

import cv2
import numpy as np

ROLE_COLORS = {"primary": (0, 220, 0), "anchor": (255, 160, 0),
               "confuser": (0, 210, 255)}
_FONT = cv2.FONT_HERSHEY_SIMPLEX
_PINGFANG = "/System/Library/Fonts/PingFang.ttc"

try:  # Pillow ships with ultralytics; optional CJK banner support.
    from PIL import Image, ImageDraw, ImageFont
    _CJK_FONT = ImageFont.truetype(_PINGFANG, 18) if Path(_PINGFANG).exists() else None
except Exception:  # pragma: no cover - environment without Pillow
    _CJK_FONT = None


def _banner_text(frame: np.ndarray, lines: list[str]) -> None:
    if _CJK_FONT is not None:
        image = Image.fromarray(frame[..., ::-1])
        draw = ImageDraw.Draw(image)
        for i, line in enumerate(lines):
            draw.text((8, 6 + 24 * i), line, font=_CJK_FONT, fill=(255, 255, 255))
        frame[:] = np.asarray(image)[..., ::-1]
    else:
        for i, line in enumerate(lines):
            ascii_line = line.encode("ascii", "replace").decode()
            cv2.putText(frame, ascii_line, (8, 24 + 24 * i), _FONT, 0.6,
                        (255, 255, 255), 2)


def draw_overlay(
    frame_bgr: np.ndarray,
    *,
    detections: Sequence,
    step_id: str,
    instruction: str,
    score: float,
    threshold: float,
    pending_question: str | None,
    recent_events: Sequence[str],
    color_text: str | None,
) -> None:
    height, width = frame_bgr.shape[:2]
    cv2.rectangle(frame_bgr, (0, 0), (width, 58), (32, 32, 32), -1)

    for det in detections:
        x1, y1, x2, y2 = det.box
        color = ROLE_COLORS.get(det.role, (200, 200, 200))
        cv2.rectangle(frame_bgr, (x1, y1), (x2, y2), color, 2)
        cv2.putText(frame_bgr, f"{det.canonical_label} {det.conf:.2f}",
                    (x1, max(70, y1 - 6)), _FONT, 0.5, color, 1)

    bar_w = int((width - 16) * min(score / max(threshold, 1e-6), 1.0))
    cv2.rectangle(frame_bgr, (8, 48), (8 + bar_w, 54), (0, 220, 0), -1)
    cv2.rectangle(frame_bgr, (8, 48), (width - 8, 54), (90, 90, 90), 1)

    lines = [f"{step_id}  score {score:.2f}/{threshold:.2f}", instruction]
    if pending_question:
        lines[1] = f"? {pending_question}"
    _banner_text(frame_bgr, lines)

    y = height - 10
    for text in list(recent_events)[-3:]:
        cv2.putText(frame_bgr, text, (8, y), _FONT, 0.45, (0, 200, 255), 1)
        y -= 18
    if color_text:
        cv2.putText(frame_bgr, color_text, (8, 74), _FONT, 0.5, (0, 255, 255), 1)


class AnnotatedVideoWriter:
    def __init__(self, path: Path, fps: float, frame_size: tuple[int, int]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._writer = cv2.VideoWriter(
            str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, frame_size)
        if not self._writer.isOpened():
            raise RuntimeError(f"cannot open VideoWriter for {path}")
        self._size = frame_size
        self.frames_written = 0

    def write(self, frame_bgr: np.ndarray) -> None:
        height, width = frame_bgr.shape[:2]
        if (width, height) != self._size:
            frame_bgr = cv2.resize(frame_bgr, self._size)
        self._writer.write(frame_bgr)
        self.frames_written += 1

    def close(self) -> None:
        self._writer.release()
```

- [ ] **Step 4: 跑测试确认通过 + Commit**

Run: `.venv/bin/python -m pytest tests/test_pipeline_render.py tests/ -q` → 全绿

```bash
git add server/pipeline/render.py tests/test_pipeline_render.py
git commit -m "pipeline: annotated overlay renderer and mp4 writer"
```

---

### Task 7: render 接入 runner + 端到端确定性回归测试(OpenCode)

**Files:**
- Modify: `harness/run_pipeline.py`(加 `--render/--no-render`,默认 `--render`)
- Test: `tests/test_pipeline_e2e.py`

**Interfaces:**
- Consumes: Task 5 的 `run(args)` 与 CLI 契约;Task 6 的 `AnnotatedVideoWriter` / `draw_overlay`。
- Produces: run 目录新增 `annotated.mp4`;`meta.json` 增加 `"annotated_frames": int`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_pipeline_e2e.py
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
```

并在 `pytest.ini` 不存在的情况下创建(仓库根):

```ini
# pytest.ini
[pytest]
markers =
    e2e: slow end-to-end pipeline tests (YOLO on CPU, ~1-2 min)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_pipeline_e2e.py -v -m e2e`
Expected: FAIL(meta 里没有 `annotated_frames`,run 目录没有 annotated.mp4)

- [ ] **Step 3: 在 runner 中接入渲染**

`harness/run_pipeline.py` 改动点(全部列出,别处不动):

1. import 区加:

```python
from server.pipeline.render import AnnotatedVideoWriter, draw_overlay
```

2. `build_parser` 加:

```python
    ap.add_argument("--render", action=argparse.BooleanOptionalAction, default=True)
```

3. `run()` 里打开视频拿到 fps 后:

```python
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    writer = (AnnotatedVideoWriter(paths.annotated, fps=fps,
                                   frame_size=(width, height))
              if args.render else None)
    recent_event_texts: list[str] = []
```

4. `emit()` 内 interaction 事件也进 ticker:在 `emit` 顶部加

```python
        if envelope.type == "perception.hand_object_relation":
            recent_event_texts.append(
                f"{envelope.payload.get('relation')} "
                f"{envelope.payload.get('hand')}/{envelope.payload.get('object_class')}")
```

(`recent_event_texts` 通过闭包捕获,同 `transitions` 的方式。)

5. 主循环 `frame_idx += 1` 之前加:

```python
            if writer is not None:
                step = engine.current_step
                draw_overlay(
                    frame,
                    detections=latest_canon,
                    step_id=engine.context.current_step_id,
                    instruction=step.instruction,
                    score=engine.context.step_progress.score,
                    threshold=step.completion_policy.threshold,
                    pending_question=(engine.context.pending_question.question
                                      if engine.context.pending_question else None),
                    recent_events=recent_event_texts,
                    color_text=(f"color={color_state}" if color_state else None),
                )
                writer.write(frame)
```

注意渲染必须在 `cv2.imwrite` 关键帧**之后**调用,保证 keyframes 存的是原始帧(未来给 VLM 用,不能带框)。核对 Task 5 代码中关键帧写盘位置,如顺序不对,把渲染块放在关键帧块后面。

6. `finally` 里 `hand_tracker.close()` 后加 `writer and writer.close()`;meta 加:

```python
        "annotated_frames": writer.frames_written if writer else 0,
```

- [ ] **Step 4: 跑 e2e 确认通过 + 全量回归**

Run: `.venv/bin/python -m pytest tests/test_pipeline_e2e.py -v -m e2e`(预计 1-2 分钟)
Expected: PASS
Run: `.venv/bin/python -m pytest tests/ -q -m "not e2e"` → 其余全绿

- [ ] **Step 5: Commit**

```bash
git add harness/run_pipeline.py tests/test_pipeline_e2e.py pytest.ini
git commit -m "pipeline: annotated mp4 in runner + deterministic e2e regression test"
```

---

### Task 8: 真实 VLM 钩子(Codex)

**Files:**
- Create: `server/pipeline/vlm_hook.py`
- Modify: `harness/run_pipeline.py`(加 `--vlm off|gemini`,默认 off)
- Test: `tests/test_pipeline_vlm_hook.py`

**Interfaces:**
- Consumes: `server/vlm` 全部契约;Task 5 的 `emit` 闭环。
- Produces: `VLMConfirmer(client, *, min_gap_ms: float = 10_000.0)`,方法
  `maybe_confirm(context, step, frame_bgr, *, session_id: str, seq: int, pts_ms: float, frame_idx: int) -> EventEnvelope | None`。
  触发条件:该步存在 `pending_question` 或 `score >= question_min_score`,且距该步上次 VLM 调用 ≥ min_gap_ms(视频时间)。`--vlm gemini` 模式明确**不保证**确定性(计入 meta:`"vlm_mode": "gemini"`)。

- [ ] **Step 1: 写失败测试(stub client,不碰网络)**

```python
# tests/test_pipeline_vlm_hook.py
from __future__ import annotations

from datetime import UTC, datetime

import numpy as np

from server.engine import StateEngine, load_recipe
from server.pipeline.session import SESSION_EPOCH
from server.pipeline.vlm_hook import VLMConfirmer
from server.vlm.schema import VLMObservation


class StubClient:
    def __init__(self):
        self.calls = []

    def analyze_image(self, request, image_bytes, *, mime_type="image/jpeg"):
        self.calls.append(request)
        return VLMObservation(
            decision_id=request.decision_id, step_id=request.step_id,
            context_version=request.context_version, frame_id=request.frame_id,
            phase="likely_complete", confidence=0.9, reason="stub",
        )


def _engine() -> StateEngine:
    return StateEngine(session_id="ses_v", recipe=load_recipe("sop/tomato_egg.json"),
                       started_at=SESSION_EPOCH)


def test_confirmer_calls_once_then_respects_min_gap():
    engine, client = _engine(), StubClient()
    confirmer = VLMConfirmer(client, min_gap_ms=10_000.0)
    frame = np.zeros((60, 80, 3), dtype=np.uint8)
    # score 0 < question_min_score 0.3 -> below band, no call
    assert confirmer.maybe_confirm(
        engine.context, engine.current_step, frame, session_id="ses_v",
        seq=0, pts_ms=0.0, frame_idx=0, force_band=False) is None

    env = confirmer.maybe_confirm(
        engine.context, engine.current_step, frame, session_id="ses_v",
        seq=0, pts_ms=1000.0, frame_idx=30, force_band=True)
    assert env is not None
    assert env.type == "vlm.step_assessment"
    assert env.payload["phase"] == "likely_complete"
    assert len(client.calls) == 1

    # 5s later: still inside min gap -> no second call
    assert confirmer.maybe_confirm(
        engine.context, engine.current_step, frame, session_id="ses_v",
        seq=1, pts_ms=6000.0, frame_idx=180, force_band=True) is None
    # 11s later -> allowed again
    assert confirmer.maybe_confirm(
        engine.context, engine.current_step, frame, session_id="ses_v",
        seq=1, pts_ms=12_000.0, frame_idx=360, force_band=True) is not None
```

(`force_band: bool = False` 是给测试用的旁路,生产路径传默认值。)

- [ ] **Step 2: 跑测试确认失败** → `ModuleNotFoundError`

- [ ] **Step 3: 实现 `server/pipeline/vlm_hook.py`**

```python
"""Trigger real VLM keyframe confirmation from engine uncertainty."""

from __future__ import annotations

from datetime import UTC, datetime

import cv2
import numpy as np

from server.events import EventEnvelope
from server.vlm.schema import VLMDecisionRequest, validate_observation

from .session import t_server_for


class VLMConfirmer:
    def __init__(self, client, *, min_gap_ms: float = 10_000.0) -> None:
        self._client = client
        self._min_gap_ms = min_gap_ms
        self._last_call_ms: dict[str, float] = {}

    def maybe_confirm(self, context, step, frame_bgr: np.ndarray, *,
                      session_id: str, seq: int, pts_ms: float, frame_idx: int,
                      force_band: bool = False) -> EventEnvelope | None:
        in_band = (
            context.pending_question is not None
            or context.step_progress.score >= step.completion_policy.question_min_score
        )
        if not (in_band or force_band):
            return None
        if not force_band and not in_band:
            return None
        last = self._last_call_ms.get(step.id)
        if last is not None and pts_ms - last < self._min_gap_ms:
            return None
        self._last_call_ms[step.id] = pts_ms

        frame_id = f"frame_{frame_idx:06d}"
        request = VLMDecisionRequest.create(
            decision_id=f"dec_{session_id}_{seq}_vlm",
            session_id=session_id,
            step_id=step.id,
            context_version=context.context_version,
            frame_id=frame_id,
            requested_at=t_server_for(pts_ms),
            completion_check=step.completion_check,
            expected_objects=step.objects_involved,
        )
        ok, encoded = cv2.imencode(".jpg", frame_bgr,
                                   [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            return None
        observation = self._client.analyze_image(request, encoded.tobytes())
        validated = validate_observation(request, observation,
                                         received_at=datetime.now(UTC))
        return validated.to_event(seq=seq, t_device_ms=pts_ms,
                                  source="gemini_vlm_pipeline_v1")
```

实现时把 `in_band` 判断整理干净(测试里 `force_band=False` 且 score 0 时必须返回 None;`force_band=True` 时跳过 band 判断,只受 min_gap 限制)。

- [ ] **Step 4: 接入 runner**

`harness/run_pipeline.py`:parser 加 `ap.add_argument("--vlm", choices=["off", "gemini"], default="off")`;`run()` 初始化区:

```python
    confirmer = None
    if args.vlm == "gemini":
        from server.pipeline.vlm_hook import VLMConfirmer
        from server.vlm.client import GeminiVLMClient
        confirmer = VLMConfirmer(GeminiVLMClient())
```

关键帧块末尾(snapshot 写完后):

```python
                if confirmer is not None:
                    vlm_env = confirmer.maybe_confirm(
                        engine.context, engine.current_step, frame,
                        session_id=session_id, seq=seq, pts_ms=pts_ms,
                        frame_idx=frame_idx)
                    if vlm_env is not None:
                        emit(vlm_env)
```

meta 加 `"vlm_mode": args.vlm`。

- [ ] **Step 5: 测试 + 回归 + Commit**

Run: `.venv/bin/python -m pytest tests/test_pipeline_vlm_hook.py tests/ -q -m "not e2e"` → 全绿
真实调用验收(需要 `.env` 里有 GEMINI_API_KEY,由用户或 Codex 实机执行,结果记入 PROGRESS):

```bash
.venv/bin/python harness/run_pipeline.py --source data/test_videos/synthetic_smoke.mp4 --device cpu --vlm gemini --max-frames 90 --keyframe-interval 1.0
```

```bash
git add server/pipeline/vlm_hook.py harness/run_pipeline.py tests/test_pipeline_vlm_hook.py
git commit -m "pipeline: gated real-VLM confirmation hook behind --vlm gemini"
```

---

### Task 9: 报告生成 + 旧 logger 废弃标注(OpenCode)

**Files:**
- Create: `server/pipeline/report.py`
- Modify: `harness/run_pipeline.py`(收尾时调用报告生成)
- Modify: `perception/session_logger.py`(仅 docstring 加废弃说明,代码零改动)
- Test: `tests/test_pipeline_report.py`

**Interfaces:**
- Consumes: run 目录内 `meta.json` + `timeline.jsonl` + `events.jsonl`。
- Produces: `write_report(paths: SessionPaths) -> Path`(生成 `report.md` 并返回路径)。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_pipeline_report.py
from __future__ import annotations

import json
from pathlib import Path

from server.pipeline.report import write_report
from server.pipeline.session import SessionPaths


def _seed(root: Path) -> SessionPaths:
    paths = SessionPaths(root=root)
    root.mkdir(parents=True)
    (root / "keyframes").mkdir()
    paths.meta.write_text(json.dumps({
        "session_id": "ses_x", "video": "v.mp4", "sop": "sop/tomato_egg.json",
        "fps": 30.0, "frames": 90, "events": 20, "vlm_mode": "off",
        "final_step_id": "step_04_combine_and_plate", "final_status": "completed",
        "transitions": [
            {"decision_id": "d1", "completed_step_id": "step_01_prepare",
             "next_step_id": "step_02_scramble_egg", "score": 0.7, "pts_ms": 600.0},
        ],
    }, ensure_ascii=False))
    paths.timeline.write_text(json.dumps({
        "pts_ms": 0.0, "frame_idx": 0, "step_id": "step_01_prepare",
        "context_version": 1, "score": 0.0, "pending_question": None,
        "detections": [["bowl", 0.7]], "color_state": None,
        "jpg": "kf_000000_0ms.jpg", "diff": {"baseline": True},
    }, ensure_ascii=False) + "\n")
    paths.events.write_text("")
    return paths


def test_report_contains_transitions_and_keyframe_table(tmp_path: Path):
    paths = _seed(tmp_path / "run_a")
    out = write_report(paths)
    text = out.read_text(encoding="utf-8")
    assert out == paths.report
    assert "step_01_prepare" in text
    assert "step_02_scramble_egg" in text
    assert "kf_000000_0ms.jpg" in text
    assert "completed" in text
```

- [ ] **Step 2: 跑测试确认失败** → 模块不存在

- [ ] **Step 3: 实现 `server/pipeline/report.py`**

```python
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
```

- [ ] **Step 4: 接入 runner + 废弃标注**

`harness/run_pipeline.py` 的 `run()` 在写完 meta 之后加:

```python
    from server.pipeline.report import write_report
    print(f"report -> {write_report(paths)}")
```

`perception/session_logger.py` docstring 第一段后追加一行(其余不动):

```python
"""...(原文)

DEPRECATED for pipeline use: harness/run_pipeline.py writes validated
EventEnvelope JSONL via server.events; this legacy flat format remains only
for harness/live_perception.py webcam smoke runs.
"""
```

- [ ] **Step 5: 测试 + 全量回归 + Commit**

Run: `.venv/bin/python -m pytest tests/ -q -m "not e2e"` → 全绿;抽跑一次 e2e 确认 report 生成不破坏确定性:`.venv/bin/python -m pytest tests/test_pipeline_e2e.py -m e2e -q` → PASS

```bash
git add server/pipeline/report.py harness/run_pipeline.py perception/session_logger.py tests/test_pipeline_report.py
git commit -m "pipeline: per-run markdown report + legacy logger deprecation note"
```

---

## 终验矩阵(Claude 执行,全部勾掉才算"MP4 全流程跑通")

- [ ] `.venv/bin/python -m pytest tests/ -q` 全绿(含新增 ~15 项)
- [ ] `.venv/bin/python -m pytest tests/ -q -m e2e` 通过(确定性 + 全产物)
- [ ] 同一 MP4 两次运行 `replay compare` 输出 `equal`
- [ ] `--script` 模式:4 次 STEP DONE,`final=completed`,`report.md` 有 4 行 transition
- [ ] `--vlm gemini` 模式:真实 VLM 至少 1 次被触发并作为证据入 events.jsonl(实机,需 key)
- [ ] `harness/clean_sessions.py --all` 能一键清空所有 run 产物
- [ ] 标注 MP4 可播放,帧数与输入一致,关键帧 jpg 是无标注原始帧

## 计划外但已知的后续(不在本计划做,防止 scope 蔓延)

1. **真实做菜视频验收(用户人工):** 按 `data/README.md` 录一段真番茄炒蛋第一人称视频,跑 `--vlm gemini --device mps`,人工看 report 和 annotated.mp4,把阈值调整记录进 PROGRESS。这是本计划产出的第一个真实用途。
2. Live stream 化:把 `run_pipeline.py` 的 `cv2.VideoCapture(file)` 换成摄像头/MJPEG 即为实时形态(perception 契约不变,这正是本计划刻意保持的)。
3. Gemini Live 生产形态(audio-only + get_current_step 工具读 SessionContext)、Supabase 落库、标注导出:见 Runbook Step 4/6。
4. ESP32 边缘形态:KeyframeSampler 的 interval 即未来端侧拍照上行周期;`clean_sessions` 对应端侧"做完即删"策略。
