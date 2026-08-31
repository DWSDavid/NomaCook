# Tomato-to-Fridge 垂直任务推进路线

> **For agentic workers:** 本文件决定阶段顺序和 Go/No-Go gate。每个阶段开始前，必须先使用 `superpowers:writing-plans` 写出该阶段的独立 implementation plan；执行时使用 `superpowers:subagent-driven-development` 或 `superpowers:executing-plans`。

**目标：** 在胸前第一人称视角下，可靠判断用户是否把桌上的番茄放进冰箱，并提供低延迟、可打断、不过度说话的实时辅助。

**架构：** 采用 event-first hybrid pipeline。CV、VLM 和语音模型只产生证据，只有 StateEngine 能改变任务状态；Redis 保存 hot snapshot 和 event stream，离线 evaluator 不得写入 live state。

**当前基线：** 仓库已经有 `EventEnvelope`、JSONL `EventLog`、deterministic replay、`StateEngine`、YOLO-World `ObjectDetector`、MediaPipe `HandTracker`、`InteractionTracker`、Gemini VLM hook 和 live camera demo。Focused baseline 为 29 个测试通过。当前没有 Redis、FastAPI runtime、Qwen adapter、clip VLM、完整 recovery graph 或 tomato-to-fridge SOP。

---

## 总原则

1. 每个阶段只解决一类风险；gate 未通过，不进入下一阶段。
2. 先使用录制视频做 deterministic replay，再运行 live camera。
3. Qwen、Redis 和 VLM 都不能用来掩盖基础 CV 或 StateEngine 的错误。
4. 任何无法确认的状态都输出 `UNCERTAIN`，不能猜测完成。
5. 所有实验模型只在 `SHADOW` 或 `REPLAY_EVAL` 中运行。
6. 每个任务都采用 TDD：先写失败测试，再写最小实现。
7. 每个实现任务独立提交，不能使用 `git add -A`。

## 阶段顺序

```text
Stage 0  固定 baseline 和 Task Contract
   ↓
Stage 1  StateEngine + golden replay
   ↓
Stage 2  Offline CV evidence
   ↓
Stage 3  Silent live execution
   ↓
Stage 4  Gateway + Redis memory
   ↓
Stage 5  Qwen realtime voice
   ↓
Stage 6  Triggered VLM + intervention
   ↓
Stage 7  24-run acceptance + 30-minute soak
```

## Stage 0：固定 baseline 和任务定义

### 要做什么

- 固定当前 focused test baseline；
- 新建 `tomato_to_fridge_v1` Task Contract；
- 明确 8 个状态、recovery transition、event vocabulary 和完成条件；
- 建立不依赖真实模型的 synthetic event fixture。

### 产物

- `sop/tomato_to_fridge.json`
- `tests/fixtures/tomato_to_fridge/happy_path.jsonl`
- `tests/fixtures/tomato_to_fridge/put_back_on_table.jsonl`
- `tests/fixtures/tomato_to_fridge/occluded_release.jsonl`
- `tests/test_tomato_to_fridge_contract.py`

### Gate

- SOP schema validation 通过；
- 同一 event log 每次 replay 得到完全相同的 snapshot sequence；
- 单帧 `OBJECT_ENTERED_REGION` 不能完成任务；
- 番茄重新离开冰箱会撤销 candidate completion。

## Stage 1：StateEngine 与 deterministic replay

### 要做什么

- 将当前线性 recipe progress 扩展成支持显式 transition 和 recovery 的 task graph；
- 为 evidence 增加 source、TTL、contradiction 和 retraction；
- 从 `SessionContext` 投影出稳定、精简的 `TaskSnapshot`；
- 建立 `RUN`、`SHADOW`、`REPLAY_EVAL` 的写权限边界。

### 产物

- `server/engine/task_graph.py`
- `server/engine/snapshot.py`
- 更新 `server/engine/sop.py`
- 更新 `server/engine/engine.py`
- `tests/test_task_graph.py`
- `tests/test_task_snapshot.py`
- `tests/test_tomato_to_fridge_replay.py`

### Gate

- duplicate、out-of-order 和 stale event 不能推进状态；
- 至少两个独立 evidence source 才能确认关键 transition；
- contradiction 可以回退到上一个 stable state；
- shadow event 永远不能写 live state；
- happy path、put-back 和 occlusion fixture 全部 deterministic。

## Stage 2：Offline CV evidence

### 要做什么

- 先录制和标注 30 段第一人称视频；
- 用相同 holdout clip 比较 YOLOE-26n、YOLOE-26s 和现有 YOLO-Worldv2；
- MediaPipe 只输出 hand geometry；
- tracker 保持 tomato identity；
- relation layer 产生 hand-near、holding、shared-motion、ROI crossing 和 release evidence；
- 所有输出转换成现有 `EventEnvelope`。

### 产物

- `domain_packs/kitchen/tomato_to_fridge.yaml`
- `perception/observations.py`
- `perception/tracking.py`
- `perception/regions.py`
- `perception/task_events.py`
- `harness/eval_tomato_to_fridge.py`
- CV benchmark report 和 frozen threshold file

### Gate

- 只在 holdout set 上选择模型和 threshold；
- detector 达到团队预先锁定的 precision/recall gate；
- 关键错误 event 不能独立造成 completion；
- p95 fast-perception latency 满足目标机器预算；
- 录制 clip 可以稳定重放成同一 evidence sequence。

如果 detector 在 holdout 上失败，停在这里。不要接 Qwen、Redis 或 live voice。

## Stage 3：Silent live execution

### 要做什么

- 复用 `server/live/frame_source.py` 接 webcam/ESP32；
- 建立 latest-frame-wins worker；
- 把实时 CV event 送入 StateEngine；
- 显示 developer-only 状态，但完全不说话；
- 写入本地 JSONL event、snapshot 和 latency artifact。

### 产物

- `server/runtime/mode.py`
- `server/runtime/session.py`
- `harness/live_tomato_to_fridge.py`
- `tests/test_runtime_mode.py`
- `tests/test_live_task_session.py`

### Gate

- 不出现 video queue accumulation；
- state update p95 小于 500 ms，不含 VLM；
- 10 次 live happy path 中 false completion 为 0；
- camera reconnect 不会建立第二个 task state；
- `SHADOW` output 无法进入 live StateEngine。

## Stage 4：Gateway 与 10–20 分钟任务记忆

### 要做什么

- 增加 FastAPI session/WebSocket gateway；
- audio 保序，video 丢旧留新；
- Redis Streams 保存 event，Redis Hash/RedisJSON 保存最新 snapshot；
- 本地 JSONL 作为 Redis outage WAL；
- reconnect 只恢复 snapshot、最近相关 event 和 pending question。

### 产物

- `server/gateway/app.py`
- `server/gateway/sessions.py`
- `server/memory/store.py`
- `server/memory/redis_store.py`
- `server/memory/wal.py`
- `tests/test_gateway.py`
- `tests/test_memory_store.py`
- `tests/test_session_restore.py`

### Gate

- 20 分钟 session 的 lookup latency 不随 event 数量线性增长；
- transport reconnect 后 2 秒内恢复 snapshot；
- Redis 暂时不可用时 StateEngine 继续运行，并在恢复后补传 WAL；
- duplicate replay 不会重复推进 state；
- 不把完整 conversation history 当作任务记忆。

## Stage 5：Qwen realtime voice

### 要做什么

- 定义 provider-neutral `RealtimeConversationAdapter`；
- 使用现有 `websockets` 依赖接 Qwen Omni Realtime；
- 只提供 read-only snapshot 和 user-intent tools；
- 接入 server VAD、streaming audio、barge-in 和本地 playback cancellation；
- provider 断线时 StateEngine 继续运行。

### 产物

- `server/voice/base.py`
- `server/voice/qwen_realtime.py`
- `server/voice/tools.py`
- `server/voice/playback.py`
- `tests/test_qwen_realtime_adapter.py`
- `tests/test_voice_tool_permissions.py`
- `tests/test_barge_in.py`

### Gate

- Qwen 没有 `advance_step()` 或 `mark_complete()`；
- 每次回答前读取最新 snapshot；
- first-audio p95 小于 1.2 秒；
- barge-in 到停止播放 p95 小于 300 ms；
- provider timeout 不改变 task state。

## Stage 6：Triggered VLM 与 Intervention Policy

### 要做什么

- 将当前单帧、周期式 VLM hook 改为 2–4 秒 ring-buffer clip；
- 只有 `UNCERTAIN_TIMEOUT`、occlusion、conflict 或用户明确提问时调用；
- response 必须绑定 session、step、context version、decision ID 和 TTL；
- Intervention Policy 管理 cooldown、dedupe、severity 和“一次只问一个问题”。

### 产物

- `server/vlm/clip_buffer.py`
- `server/vlm/confirmer.py`
- `server/intervention/policy.py`
- `tests/test_clip_buffer.py`
- `tests/test_triggered_vlm.py`
- `tests/test_intervention_policy.py`

### Gate

- ordinary on-track action 不调用 VLM；
- stale VLM response 被拒绝；
- 同一个问题不会重复播报；
- VLM timeout 只产生 `UNCERTAIN`，不能完成任务；
- 每个成功 run 的 unnecessary intervention 不超过 1 次。

## Stage 7：集成验收

### 24 次锁定验收

- 8 次标准执行；
- 8 次自然变化、遮挡和干扰物；
- 8 次 deliberate deviation/recovery。

### 必须通过的指标

| 指标 | Gate |
|---|---:|
| False task completion | 0 / 24 |
| 合法任务正确完成 | 至少 15 / 16 |
| 偏离被检测或询问澄清 | 至少 7 / 8 |
| 成功任务的多余语音 | 每次不超过 1 条 |
| Fast evidence 到 state update | p95 < 500 ms |
| First speech audio | p95 < 1.2 s |
| Barge-in 停止播放 | p95 < 300 ms |
| Reconnect 恢复 | < 2 s |
| Evaluator 写入 live state | 0 |

随后运行 20–30 分钟 soak，覆盖断网、Redis timeout、Qwen timeout、VLM timeout、camera reconnect、暂停恢复、左右手和不同执行速度。

## 什么时候才能开始第二个应用场景

只有 Stage 7 通过后，才增加第二个 kitchen assignment，例如“把鸡蛋从冰箱拿到桌面”。第二个任务必须只新增 domain pack、Task Contract 和少量 task-specific evidence rule；如果需要重写 gateway、voice、memory 或 StateEngine，说明当前架构还没有真正模块化。

## 当前立刻执行的下一步

先执行 Stage 0–1，不接真实 Qwen、不接 Redis、不更换 detector。对应的详细 TDD implementation plan：

- `docs/superpowers/plans/2026-08-09-tomato-to-fridge-state-foundation.md`
