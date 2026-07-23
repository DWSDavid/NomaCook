# NomaChef 最终架构与技术栈 (Final)

> 2026-07-23 | 完整版: repo `docs/` 下 TECHNICAL-SPEC / FINAL-STACK-DATA-REPO-PLAN / IMPLEMENTATION-RUNBOOK

## 1. 架构主线

```text
DFRobot ESP32-S3 AI CAM (DFR1154, 头戴式第一人称)
  摄像头 / PDM 麦克风 / MAX98357 扬声器 / microSD 缓存 / Wi-Fi
        ↓ MJPEG 640x480@5-10fps + 16kHz PCM 上行, PCM 下行(兼 AEC reference)
Session Server (FastAPI, Gateway+Ingest 合并)
  设备鉴权 / 时钟映射(t_device_ms -> t_server_est) / Gemini Live 后端代理
        ↓ 最新帧丢弃策略, 过期帧不进推理
Fast Perception Worker (独立进程)
  YOLO-World(按 SOP 步骤动态英文词表) + MediaPipe Hand Landmarker(21 点, grip_closure)
  产出 hand_object_relation 弱证据(det_conf / grip_closure / overlap 分离携带)
        ↓
Append-only Event Log (统一事件信封)
  event_id(ULID) / session_id / seq / frame_id / source / backfill, 幂等去重, 可回放
        ↓
State Engine (唯一写者, 按 seq 单线程消费)
  证据打分 + 阈值 + 连续达标 / 超时提问 / stale 规则(frame_age>3s 不计分)
  模型永远只能"提议", advance_step 不存在
        ↓
Versioned SessionContext (context_version 自增)
  recipe_version / current_step / 证据分 / last_visual_summary / timers / pending_question
        ↓ 快照生成 prompt, Live 与 VLM 互不直连
Gemini Live (audio-only, 实时语音+工具调用) + Gemini VLM (关键帧结构化确认)
        ↓
Supabase + pgvector (session / events / media / recipe_versions / 受控 RAG)
```

## 2. 关键设计决策

| 决策 | 结论 |
|---|---|
| 机位 | 头戴式第一人称(与 EPIC/Ego4D 同类视角, 公开数据参考价值上升); 160° 广角服务端去畸变+裁边 |
| Hands vs Pose | **Hand Landmarker 保持主线**。Pose 每手仅 3-4 粗略点, 无法算 grip_closure; 头戴画面拍不到躯干。Pose 仅作上肢补充评估项 |
| Live 接入 | 统一经后端代理(审计/重试/上下文同步); ephemeral token 直连留待后期。audio-only 15min 限制 + 单连接约 10min, 用 context compression + session resumption; 传输断连用 resumption, 上下文失同步开新 session 注入快照, 两路径不混用 |
| 工具调用 | 只读工具 + user_confirm_step(需 transcript 绑定校验) + request_visual_check(立即返回占位, VLM 结果后置注入; 3.1 Live 不支持 NON_BLOCKING) |
| VLM | 低频/事件触发, 请求带 decision_id+step_id+context_version, 返回不符即标 stale 落库不计分, TTL 8s |
| RAG | 只在 session 前(选菜+SOP 冻结为 recipe_version/sop_snapshot)和白名单知识问答时调用; 结果永不触碰状态机; 过敏/温度走确定性规则 |
| 音频事件层 | 保留(规格曾遗漏): MVP 用音量+频带规则, YAMNet(Apache-2.0)备选, 作为 evidence 进状态机 |

## 3. 技术栈与 License(能否进产品)

| 层 | 选型 | License | 状态 |
|---|---|---|---|
| 固件 | ESP-IDF + esp32-camera + ESP-SR(AEC/VAD) | Apache-2.0 / ESP-SR 限 Espressif SoC 免费 | 干净 |
| 服务端 | FastAPI + google-genai SDK + ULID | MIT / Apache-2.0 | 干净 |
| 检测 | ultralytics YOLO-World | **AGPL-3.0** | **唯一地雷**: demo 期可用; 商业化前换 Grounding DINO/OWLv2(均 Apache-2.0)或买 Enterprise |
| 手部 | MediaPipe Hand Landmarker | Apache-2.0 | 干净 |
| 音频事件 | YAMNet + Silero VAD | Apache-2.0 / MIT | 干净 |
| 语音 | Gemini Live(全 Preview!) 备胎 faster-whisper+Piper | API / MIT | 薄适配层, 模型名配置化 |
| VLM | Gemini Flash, 备胎 GLM-4.6V-Flash | API / MIT | 干净 |
| 存储 | Supabase + pgvector | Apache-2.0 | 干净 |

## 4. 数据计划(只算可直接进产品的)

- **P0 自采第一人称视频**(唯一可商用训练的视频资产): 10 段 x 2-3min 场景矩阵 + 20-30 帧 VLM fixture。头戴机位/中式调料瓶/160° 畸变在公开数据中不存在, 且确定性回放验收需要固定输入
- **P0 HowToCook**(Unlicense 公有领域): 中文结构化菜谱, "菜谱→SOP JSON"解析器语料
- **P1 免费 API**: USDA FoodData Central(公有领域, 免费 key), Open Food Facts(ODbL), TheMealDB
- **附录 A(仅内部离线评估, 永不进产品)**: CaptainCook4D(带错误标注的做菜视频, 测状态机误推进)、EPIC-SOUNDS(厨房声音 44 类)、HoloAssist(真人教练 transcript)、VISOR、100DOH、下厨房语料

## 5. 实施顺序(7 天垂直切片, 不等硬件)

1. 事件信封改造 + 回放器(同录像两遍 diff 为空)
2. State Engine + SessionContext 内存版 + 手写蛋炒饭 SOP(乱序/重复/stale 单测)
3. VLM fixture 测试(20-30 帧, stale 注入测试)
4. audio-only Live hello world(>10min 跨连接, 重连答对当前步骤)
5. 全链路闭环(5min 模拟 session, 3 步转移, 回放 diff 为空)
6. Supabase 落库 | 7. ESP32 到货后烧机矩阵(AEC 第一优先)

## 6. 风险 Top 5

1. ultralytics AGPL(商业化前必须解决, 出口明确)
2. Gemini Live 无 GA 模型且官方推荐 Interactions API(适配层要薄)
3. DFR1154 视频+音频+播放并发吞吐未知(烧机测试定, 失败升级 ESP32-P4X-EYE)
4. AEC 参考信号链路(MAX98357 无回采, 软件回环+重采样, 到货第一验证项)
5. 口头确认被模型幻觉滥用(transcript 绑定校验 + 高风险步骤问答配对)
