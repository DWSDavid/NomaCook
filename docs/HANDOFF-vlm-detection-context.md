# Handoff — 把清理后的检测结果作为 context 喂给 Gemini(给 Codex)

> 2026-07-24。用户看到 annotated 帧检测框很杂(重复 egg、三个 bowl、锅被标 bowl、
> 刀 0.23),要求把本地检测结果当 context 喂给 Gemini,但**先清理再喂**。
> 清理模块 `server/vlm/detection_context.py` 已写好并自测(16 个乱框 → 一句干净清单)。
> 你的活:把它接进 VLM 调用链,加系统提示第 7 条,并把成品识别简化成一句旁白。

## 设计红线(别破坏)

检测结果只是**给 Gemini 的背景提示,不是一张选票**。Gemini 仍独立判断,并被明确告知
"以画面为准,冲突时相信画面"。这样既降低它犯傻(锅碗混淆),又不毁掉打分制的信号独立性。

## 任务 1:接检测 context

1. `server/vlm/schema.py` 的 `VLMDecisionRequest` 增加一个可选字段:
   `detection_context: str = ""`(纯文本,清理后的那段)。放进 `create()` 参数。
2. `server/vlm/client.py` 的 system prompt 末尾加第 7 条(原文见下),并在 `analyze_image`
   拼 prompt 时,若 `request.detection_context` 非空,追加到"相关物体/失败情况"之后:
   ```
   第 7 条(加到 SYSTEM_PROMPT):
   7. 你会额外收到一段"本地检测器"的粗略识别结果作为线索。它可能有重复、误标或漏检，
      只能当提示，绝不能当结论；一切以你在图片里实际看到的为准，冲突时相信画面。
   ```
3. `server/pipeline/vlm_hook.py` 的 `maybe_confirm` 增加参数
   `detections=None, hands=None, frame_wh=None`。用**空间版**(不是光列名字):
   ```python
   from server.vlm.detection_context import format_scene_context
   ctx = ""
   if detections is not None and frame_wh is not None:
       ctx = format_scene_context(
           detections, hands or [], frame_wh, step.objects_involved)
   ```
   把 `detection_context=ctx` 传进 `VLMDecisionRequest.create(...)`。
   注:`format_scene_context` 给的是"右手拿着菜刀、番茄在左侧、锅在上方"这种
   **手部关系 + 相对位置**,这才是 Gemini 单帧推不准、真正有用的 context;
   纯物体名清单价值低,不要用旧的 `format_detection_context`。
4. `harness/run_pipeline.py` 调 `confirmer.maybe_confirm(...)` 处,多传
   `detections=latest_canon, hands=hands, frame_wh=(width, height)`。
   (`hands` 是该帧 `hand_tracker.detect(...)` 的结果,已有变量。)
5. 单测:给 detection_context.py 补 3-4 条纯函数测试(dedupe、floor 边界、
   missing_expected 归一化);现有全套保持绿。

## 任务 1.5:VLM 判别改成固定 5 秒(减少计算)

用户要求:去掉"逼近达标时提速到 3 秒"的快速档,统一固定每 5 秒判别一次。
1. `harness/run_pipeline.py`:`--vlm-interval` 默认从 9.0 改 **5.0**。
2. 构造 `VLMConfirmer(...)` 时传 `fast_gap_ms=None`(关掉快速档),
   这样无论是否逼近达标都固定 `min_gap_ms`(=5 秒)。
3. 本地关键帧采样(`--keyframe-interval`)维持 3 秒不动,只改 VLM 这一路。

## 任务 2:成品识别改成一句旁白(不加模型、不调 Gemini)

用户决定:成品菜 demo 时直接讲,不认。所以:
1. **删掉** `server/vlm/dish.py`,并从 `docs/HANDOFF-demo-log-and-dish.md` 的任务 2 里
   撤掉 Gemini 认菜那段(改为下面这句)。
2. 菜名本来就在 SOP 里(`recipe.dish`)。把结尾旁白 `complete_item` 改成带菜名:
   例如 `f"{recipe.dish}做好了。妈，我会做饭了。"`(narrate.py)。
3. demo_log 的 `dish()` 调用改为直接用 `recipe.dish`,不再依赖识别结果。

## 任务 3(可选,直接回应"信息很杂"):清爽版检测框

同一个 `curate_detections` 也能让 annotated 视频不那么乱。在 `--demo-log` 开启时,
overlay 只画 `curated.confident` 的框(去重、隐藏低置信和 confuser/anchor),
让演示画面干净。工程调试仍可用 `--no-demo-log` 看全部框。

## 验收

```bash
.venv/bin/python harness/run_pipeline.py \
  --source <最新演示视频> --narrate gemini --run-tag vlm_ctx_v1
```

1. report.md 的 Gemini observations 里,reason 质量应变好(锅碗混淆变少);
   可在 events.jsonl 抽一条 VLM 请求,确认 prompt 里带了"以下是本地检测器…"那段。
2. 结尾旁白说出"番茄炒蛋做好了。妈,我会做饭了。"
3. (可选)开 --demo-log 时 annotated 帧明显变干净。
4. 全套测试绿。

## commit(按推送规则)

```
git add server/ harness/ tests/ docs/
git rm server/vlm/dish.py
git commit -m "vlm: feed curated detector output as context; scripted dish closer"
git push origin main
```

## 实测记录(执行后填写)

- VLM prompt 是否带上 context(抽一条贴出来):是。客户端桩捕获到实际发送的
  `contents[0]` 含系统提示第 7 条及空间 context，例如
  `- 手部：右手拿着木铲`、`- 位置：炒锅在画面中间`。运行事件目前只保存响应，
  不保存 request/prompt。
- 锅碗混淆是否减少(对比 vlm_ctx 前后):`vlm_ctx_v1` 共 16 次调用，间隔
  5.000-5.033 秒；60-75 秒炒蛋阶段连续判断为“锅中倒油/倒蛋液/蛋液凝固”，
  未把灶上炒锅说成碗。
- 结尾旁白菜名是否正确:正确。真实素材在第 4 步结束，另用 7 步 scripted smoke
  闭环，`narration.json` 的 complete 项为
  `番茄炒蛋做好了。妈，我会做饭了。`。
- (可选)清爽版检测框观感:开启 `--demo-log` 后，同类框去重，只保留高置信
  primary；65 秒炒蛋画面只保留炒锅和油瓶等关键框，低置信/anchor/confuser 已隐藏。
- 遗留问题:最新 78 秒演示素材只拍到第 4 步，不能自然触发 7 步最终旁白；
  `events.jsonl` 尚不记录 VLM request，因此 prompt 证据由客户端捕获测试提供。
