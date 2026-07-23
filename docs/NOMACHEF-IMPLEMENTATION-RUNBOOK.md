# NomaChef 实施 Runbook (最短垂直切片)

> 日期: 2026-07-23 (同日调整: 三轨并行 + 硬件后置)
>
> 目标: 不等硬件, 在 Mac 上跑通
> `视频源 -> 快视觉事件 -> Event Log -> State Engine -> SessionContext -> Live 问答 -> VLM 确认 -> 状态推进 -> 语音回复 -> 完整回放`。
> 技术栈与 license 见 [NOMACHEF-FINAL-STACK-DATA-REPO-PLAN.md](./NOMACHEF-FINAL-STACK-DATA-REPO-PLAN.md)。
> 架构契约(event envelope / SessionContext / VLM output 等六个 JSON 契约)以
> cross-check 报告 E 节为准, 实现前先落成 `server/events/schema.py`。
>
> 分工: Claude 写代码和文档; 实机执行、下载、注册交给用户或 Codex/OpenCode。

## 相关仓库 (2026-07-23)

| 仓库 | 职责 | 归属 |
|---|---|---|
| [DWSDavid/NomaChef](https://github.com/DWSDavid/NomaChef) (本仓库) | Python Session Server: 实时感知 / 事件日志 / 状态机 / VLM / Live 语音 | 我们, 进度按 `perception/` `harness/` `server/` `sop/` `docs/` 分类推送 |
| [ztboxs/NomaChef-Backend](https://github.com/ztboxs/NomaChef-Backend) | Go 管理后端: 设备注册 / 健康上报 / App 查询, **不碰实时媒体与 AI** | 队友; 跨服务契约见其 `docs/integrations/session-server.md`, 对接前需双方确认 device_id/session_id/鉴权等 |

## 轨道结构 (2026-07-23 调整)

三条线并行, 互不阻塞:

- **Track A (主线)**: Step 1 → 6, 顺序不变。事件信封 → 状态引擎 → VLM → Live 语音 → 闭环 → 落库。
- **Track B (提前)**: Gemini Live 摄像头冒烟测试 (Step B1)。原本 Live 要等到
  Step 4, 但它对 Step 1-3 零依赖, 且能提前验证 API key / 模型名 / 视频帧输入 /
  语音输出 / 打断这五个最大的外部风险。**定位是管线验证, 不是生产形态**:
  生产架构仍按 spec §11.2 用 audio-only Live + VLM 分离(audio+video Live
  无压缩上下文仅约 2 分钟)。B1 的实测结论直接喂给 Step 4。
- **Track C (提前)**: 厨房词表与评测数据 (Step C1)。YOLO-World 零训练,
  数据工作的真正杠杆是①更丰富的开放词表 ②评测帧集(调 conf 阈值/验证召回)。
  属于第一层感知(前期), 不是后期工作。
- **硬件线 (后置)**: 原 Step 7 (ESP32/3D 打印外壳) 明确推迟, 等硬件与
  3D 打印支撑到位后再启动, 见文末。

## Step 0: 数据与账号准备 (Codex/用户, 可并行, 半天) — ✅ 已完成 (2026-07-23)

执行报告见 [STEP0-REPORT.md](./STEP0-REPORT.md)。Phase A/B/C/E 完成;
Phase D live 验收部分通过(FPS/JSONL 达标, holding 闭环未触发)。遗留人工项:

- [ ] 三个 key 注册填入本地 `.env` (Gemini 已有, 待填入; USDA/Supabase 待注册)
- [ ] 录制 8 场景自采视频, 存 `data/test_videos/` + 旁挂 `meta.json`
- [ ] 从自采视频抽 20-30 帧 fixture, 存 `data/test_frames/fixtures/`
- [ ] 复测 Phase D holding start/end 闭环 (手持 bottle/bowl 1-2 秒)

遗留项不阻塞 Track A Step 1 (可用合成视频顶) 与 Track B。Track C 的
评测部分依赖 fixture 帧, 词表部分不依赖。

## Step B1 (Track B): Gemini Live 摄像头冒烟测试 (0.5-1 天, 立即可做)

实现 (Claude 已写, 见 `harness/live_gemini_smoke.py`):
- Mac 摄像头帧 (默认 1 fps, JPEG) + 麦克风音频 (16 kHz PCM) 经
  `client.aio.live.connect` 送入 Live session, 播放返回的 24 kHz 音频
- 开启输入/输出 transcription, 终端实时打印双向文字稿
- system instruction: 厨房场景描述员, 中文回答"你现在看到什么"
- 模型名配置化 (`GEMINI_LIVE_MODEL`, 默认 gemini-3.1-flash-live-preview),
  模型名 404 时按 handoff 文档换名重试
- 全程 JSONL 审计日志落 `data/sessions/`

实测 (用户/Codex, 见 [HANDOFF-gemini-live-smoke.md](./HANDOFF-gemini-live-smoke.md)):
- `uv pip install -p .venv sounddevice` + `.env` 填入 GEMINI_API_KEY
- 戴耳机跑 (回声防自激); 对镜头摆几样厨房物品, 问"你看到什么"

验收: ①连接成功且能听到语音回复 ②对着摄像头提问, 回答内容与画面一致
③说话打断模型有效 ④transcript 与音频一致 ⑤JSONL 日志完整。
依赖: 无 (仅 GEMINI_API_KEY)。实测结论(模型名/延迟/打断体验)填回本节, 供 Step 4 引用。

## Step C1 (Track C): 厨房词表扩充 + 评测集 (0.5-1 天, 立即可做)

实现 (Claude 已写):
- `perception/kitchen_vocab.py`: 分类厨房词表 (炊具/工具/容器/食材/调料/
  电器/手部), 附每道 corpus 菜的 objects_involved 生成辅助
- `harness/eval_vocab.py`: 对 `data/test_frames/fixtures/` 批量跑 YOLO-World,
  输出各类目命中率/置信度分布报告, 用于调 conf 阈值和筛掉无效词
- `harness/smoke_yolo_world.py` 与 `perception/detector.py` 改用共享词表

数据 (用户/Codex, 按 DATASET-PLAN 登记流程):
- 主评测集 = 自采视频抽帧 (版权干净, 唯一可进产品的数据)
- 外部厨房数据集候选已登记在 [DATASET-PLAN.md](./DATASET-PLAN.md) 待办,
  下载前人工复核 license

验收: 词表加载进 YOLO-World 不报错; fixture 帧就绪后 eval 报告可生成;
每类词在自采帧上有非零召回或被明确标记剔除。
依赖: 词表部分无依赖; 评测部分依赖 Step 0 遗留的 fixture 帧。

## Step 1: 事件信封改造 + 回放器 (1 天)

实现:
- `server/events/schema.py`: event envelope(event_id/session_id/seq/frame_id/
  t_device_ms/t_server_est/received_at/source/schema_version/backfill/payload)
- 感知输出迁移: `fusion` 产 evidence payload(relation/phase/signals 三层置信度分离),
  `session_logger` 写 envelope 格式; `t` 一律换成帧时间戳, 不再用 time.time()
- `harness/replay.py`: 读视频或 JSONL, 重放出逐位一致的事件流
- 顺手修: `hands.py` 的合成 33ms 时钟改为真实帧时间戳; `DETECT_EVERY` 改按时间间隔

验收: 同一录像跑两遍, 事件流(去掉 received_at)diff 为空; 旧 JSONL 有一次性迁移脚本或明确废弃声明。
依赖: Step 0 的录像(可先用 data/test_videos/synthetic_smoke.mp4 顶)。
暂不做: Supabase 落库、目录大迁移。

## Step 2: State Engine + SessionContext 内存版 (1-2 天)

实现:
- `sop/schema.json` + 手写一道蛋炒饭 `sop/fried_rice.json`(completion_check 只写静态可判条件)
- `server/engine/`: 证据打分、阈值+连续达标、超时提问(pending_question)、
  context_version 自增、单写者按 seq 消费
- 口头确认规则: 只作为高权重证据, 需 transcript 绑定位; 高风险步骤问答配对(先留接口)
- stale 规则: frame_age > 3s 的证据只落库不计分

验收: 喂 Step 1 的事件 JSONL, 步骤转移序列确定且每次转移有 evidence_refs;
单测覆盖乱序、重复 event_id、stale 事件、单低置信度事件不能推进。
依赖: Step 1。
暂不做: 真语音、计时器语音播报。

## Step 3: VLM 关键帧确认 (1 天)

实现:
- `server/vlm/`: Gemini Flash structured output, 请求带 decision_id/step_id/context_version
- 后端校验: 枚举、范围、step_id 与 context_version 比对, 不符标 stale 落库
- fixture 测试: 20-30 张固定帧, 同图 3 次调用关键字段稳定性报告
- TTL 8s 超时放弃

验收: fixture 全部返回合法 schema; 人工注入延迟的 stale 结果被正确丢弃且有审计记录。
依赖: Step 2 的 decision 接口; Step 0 的 fixture 帧。

## Step 4: audio-only Gemini Live hello world (1-2 天)

实现:
- `server/voice/`: 后端代理连接 Live(模型名配置化, 默认 gemini-3.1-flash-live-preview)
- 只挂两个只读工具: get_current_step / repeat_instruction
- request_visual_check 采用"立即返回 {status: checking}, VLM 结果后置注入触发新回复"
  模式(3.1 Live 不支持 NON_BLOCKING, 见 cross-check issue #2)
- session resumption: 传输断连用 resumption handle 重连; 上下文失同步则开新
  session 并注入最新 SessionContext 快照, 两种路径不混用
- model_calls 审计落 JSONL

验收: 连续对话 > 10 分钟(跨连接上限), 重连后答对"现在第几步";
打断(barge-in)可用; 工具调用有 dedupe_key 去重。
依赖: Step 2。
暂不做: ESP32 音频、AEC、ephemeral token、faster-whisper 降级链路。

## Step 5: 全链路闭环 (1-2 天)

实现:
- Mac 摄像头或录像作视频源, Step 1-4 串联
- 用户口头确认路径: transcript 校验 -> user_confirm_step 证据 -> 状态机推进
- 端到端事件全部入同一 event log, 回放器可完整重演

验收: 一次 5 分钟模拟做菜 session, 无人工干预完成 3 个步骤转移,
含一次 VLM 确认和一次口头确认; 回放 diff 为空。
依赖: Step 1-4。

## Step 6: Supabase 落库 (0.5-1 天, 可插在 Step 2 后任意时点)

实现: spec §13 九张表; event log 双写(本地 JSONL + Supabase); 媒体走 signed URL + RLS。
验收: 一次 session 后, Supabase 中 events/media/model_calls 可查且与本地 JSONL 行数一致。

## Step 7: 硬件线 (明确后置, 2026-07-23 决定)

**推迟至硬件 (ESP32-S3 AI CAM) + 3D 打印支撑到位后再启动, 不排期。**
Mac 摄像头足以支撑 Step 1-6 与 Track B/C 的全部开发和验收。到货后再做:

- CameraWebServer MJPEG 接入 Session Server ingest(带最新帧丢弃策略, 不直接进 cap.read 循环)
- 跑 spec §15.2 烧机矩阵, 实测数据填回 spec
- AEC 参考信号闭环是第一优先验证项, 失败即启动 P4X-EYE 评估
- 3D 打印胸戴外壳与佩戴角度验证

## 里程碑后置项 (记录, 不排期)

- MediaPipe Pose 上肢点补充评估(前臂朝向证据; Hands 保持主线, 见 STACK-PLAN §1.3 决策)
- Grounding DINO/OWLv2 替换评测(解 ultralytics AGPL, 商业化前)
- 100DOH / Ego-HOIBench 对比评估(附录 A 规则)
- 音频事件层(YAMNet/规则)接入 evidence model
- faster-whisper + Piper 语音降级链路
- ephemeral token 直连架构评估
