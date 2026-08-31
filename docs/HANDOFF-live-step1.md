# Handoff — Live 第 1 步:抽出 SessionCore(纯重构,行为不变)

> 给 Codex。目标只有一个:把感知+打分的大脑从 `run_pipeline.py` 抽成 `SessionCore`,
> 让离线管线改用它,**行为一模一样,106 个测试继续全绿**。不加任何实时功能。
> 设计背景见 `docs/DESIGN-live-service.md`。基线已 tag `offline-base-v1`。
> `server/live/frame_source.py` 已写好(FrameSource / VideoFileSource /
> CameraStreamSource),本步要用上 VideoFileSource。

## 为什么先做这步

实时化的风险全在"把大函数拆开会不会拆坏"。所以先做零功能变化的重构,用现有测试当
安全网。这步过了,加实时就只是"换个 FrameSource + 加个 Gateway",不碰大脑。

## 要做的

1. 新建 `server/live/session_core.py`,定义 `SessionCore`:
   - 构造:`SessionCore(recipe, *, device="cpu", vlm="auto", k_frames=3, ...)`,
     内部持有 detector / hand_tracker / fusion / engine / (可选)VLMConfirmer,
     就是现在 run_pipeline 里那些初始化。
   - 核心方法:`step(pts_ms: float, frame_bgr) -> StepResult`,把 run_pipeline 主循环
     里"每帧做的事"搬进来:检测(按 detect_every)、手部、fusion、keyframe 采样时的
     presence/color/VLM、喂 engine、收集本帧产生的事件和状态。
   - `StepResult` 带:本帧新事件列表、当前步骤/分数/pending_question、transition(若有)、
     latest_canon(给渲染/overlay)、要播的旁白项(intro/preview/transition/remark/complete)。
   - 旁白**选择逻辑**(预告门控、remark 限流去重)也搬进来,作为 SessionCore 的方法,
     这样离线和实时共用同一套"什么时候该说话"。
2. `harness/run_pipeline.py` 改成薄壳:
   - 用 `VideoFileSource(args.source)` 取代直接的 `cv2.VideoCapture` 主循环。
   - `for pts_ms, frame in source.frames():` 里调 `core.step(pts_ms, frame)`,
     拿 StepResult 去做原来的事:写 JSONL、渲染 annotated、demo_log、narration.json。
   - 渲染、demo 日志、文件写入这些**输出**留在 run_pipeline(它们是离线 OutputSink),
     不进 SessionCore。SessionCore 只出数据,不写文件不画框。
3. 不改任何 SOP、阈值、事件格式、narrate 逻辑。纯搬家。

## 验收(硬性)

1. `pytest tests/ -q` 全绿(106 passed),一个都不能少。
2. 用 `data/test_videos/test2.mov` 各跑一次重构前(`git stash` 或 checkout tag)和重构后,
   `meta.json` 里 `transitions`、`final_step_id`、`events` 数量**完全一致**。行为不变是
   这步的全部意义,用数据证明。
3. `server/live/session_core.py` 不 import cv2 的窗口/写文件,不 import narrate 的
   ffmpeg 部分。它只吃 ndarray 吐 dataclass。

## commit(按推送规则)

```
git add server/live/ harness/run_pipeline.py docs/
git commit -m "live/step1: extract SessionCore from run_pipeline (pure refactor, behavior identical)"
git push origin main
git tag -a live-step1 -m "SessionCore extracted; offline reuses it; 106 tests green"
git push origin live-step1
```

## 下一步(本 handoff 不做)

第 2 步是最小 FastAPI Gateway:`/ws/device` 用 `CameraStreamSource` 读 MJPEG,
`/ws/app` 推 SessionCore 的状态事件。等第 1 步 tag 完再开。

## 实测记录(执行后填写)

- 重构前后 transitions / final_step_id / events 对比:
- 测试结果:
- 遗留问题:
