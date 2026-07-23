# NomaChef 最终技术栈、数据与仓库计划

> 日期: 2026-07-23
>
> 定位: 本文回答"每一层用什么、license 是否允许进产品、需要下载/注册什么"。
> 架构决策以 [NOMACHEF-TECHNICAL-SPEC.md](./NOMACHEF-TECHNICAL-SPEC.md) 为准，本文不重复论证。
> 执行步骤见 [NOMACHEF-IMPLEMENTATION-RUNBOOK.md](./NOMACHEF-IMPLEMENTATION-RUNBOOK.md)。
>
> 分类原则: **只有可以直接 access 并合法进入产品的资源才算数**。
> research-only / NC 许可的数据集一律降级到附录 A（仅限内部离线评估，永不进产品管线）。

## 0. 总体结构

```text
DFRobot ESP32-S3 AI CAM (DFR1154)
  摄像头、麦克风、扬声器、microSD 缓存、Wi-Fi
        ↓
Session Server (Device Gateway + Media Ingest 合并进程)
  设备鉴权、音视频接入、时钟映射、Gemini Live 代理
        ↓
Fast Perception Worker
  YOLO-World(或 Apache 替代) + MediaPipe, 持续产生物体/手/hand-object evidence
        ↓
Append-only Event Log
  统一事件信封, 可回放、可审计 (spec §10.1 + cross-check E 节契约)
        ↓
State Engine
  唯一可以判断步骤完成、修改状态的组件, 单写者按 seq 消费
        ↓
Versioned SessionContext
  当前菜谱版本、步骤、证据分、视觉摘要、计时器、pending_question
        ↓
Gemini Live (实时语音) + Gemini VLM (按需关键帧确认)
  两者只从 SessionContext 快照取上下文, 互不直连
        ↓
Supabase + pgvector
  session、事件、媒体、菜谱版本、受控 RAG 索引
```

## 1. 逐层技术栈与 license 判定

结论列含三档: **可进产品** / **有条件** / **不可(需替换或买license)**。

### 1.1 设备层 (ESP32-S3)

| 组件 | 选型 | License | 结论 | 获取方式 |
|---|---|---|---|---|
| 固件框架 | ESP-IDF | Apache-2.0 | 可进产品 | espressif/esp-idf |
| Arduino 备选 | arduino-esp32 | LGPL-2.1 | 可进产品(动态链接语义, 固件场景需注意) | espressif/arduino-esp32 |
| 相机驱动 | esp32-camera | Apache-2.0 | 可进产品 | espressif/esp32-camera |
| AEC/VAD/NS | ESP-SR | Espressif 自有许可, 限 Espressif SoC 上免费使用 | 可进产品(仅跑在 ESP 芯片上, 符合条件; 落地前读一遍 LICENSE 原文) | espressif/esp-sr |
| MJPEG 参考实现 | CameraWebServer 例程 | Apache-2.0 (随 arduino-esp32/esp-idf) | 可进产品(仅作起点, 生产要重写) | DFRobot wiki 示例 |

### 1.2 Session Server

| 组件 | 选型 | License | 结论 |
|---|---|---|---|
| Web 框架 | FastAPI + uvicorn | MIT / BSD | 可进产品 |
| WebSocket | starlette 内置 | BSD | 可进产品 |
| Gemini 客户端 | google-genai (官方 SDK) | Apache-2.0 | 可进产品 |
| 事件 ID | ULID (python-ulid) | MIT | 可进产品 |
| 队列(MVP) | 进程内 asyncio.Queue, 每 session 一条 | 标准库 | 可进产品; 不引入 Kafka/Redis Streams |

### 1.3 Fast Perception Worker (关键 license 决策点)

| 组件 | License | 结论 |
|---|---|---|
| **ultralytics YOLO-World (现状, `yolov8s-worldv2.pt`)** | **AGPL-3.0** | **不可直接进闭源商业产品**。三选一: (a) 买 Ultralytics Enterprise License; (b) 整个衍生后端按 AGPL 开源; (c) 替换。官方措辞: 商用需公开"完整衍生作品源代码, 含模型权重", 否则需 Enterprise License |
| YOLO-World 原始实现 (AILab-CVC) | GPL-3.0 | 同样病毒性, 不解决问题 |
| **Grounding DINO (IDEA-Research)** | Apache-2.0 | **可进产品**, 开放词汇检测首选替代; 延迟高于 YOLO-World, 需实测 |
| **OWLv2 (Google, 经 HF transformers)** | Apache-2.0 | 可进产品, 第二替代; transformers 本身 Apache-2.0 |
| MM-Grounding-DINO (OpenMMLab) | Apache-2.0 | 可进产品, 第三替代 |
| **MediaPipe Hand Landmarker** | Apache-2.0 (代码和 .task 模型) | 可进产品, **维持主线** |
| MediaPipe Pose Landmarker | Apache-2.0 | 可进产品, 但**只作评估项, 不替代 Hands**(见下方决策) |
| OpenCV (opencv-contrib-python) | Apache-2.0 | 可进产品 |
| PyTorch | BSD-3 | 可进产品 |

**Pose vs Hands 决策 (2026-07-23)**: 曾提议把 Hands 换成 Pose 以获得更全面的肢体
信息。技术上不可行, 两个原因: (a) fusion 的 `grip_closure` 依赖 Hand Landmarker 的
21 个手部关键点, Pose 每只手只有腕/拇指/食指/小指约 3-4 个粗略点, 换掉即失去
holding 判定; (b) 头戴第一人称画面拍不到自己的躯干, Pose 的全身模型在只见手臂的
画面上不可靠。结论: Hands 为主; 若实测头戴画面能稳定拍到前臂/肘, 再评估用 Pose
的上肢点补充"前臂朝向"证据(对倾倒类动作有价值), 接口仍走统一的
`hand_object_relation` evidence, 不改状态机。

**决策**: hackathon/demo 阶段继续用 ultralytics(AGPL 对 demo 无实际风险);
`ObjectDetector` 类保持现有"ndarray 进、Detection 出"接口不变, 商业化里程碑前
完成 Grounding DINO/OWLv2 的替换评测。这是一个已知的、有明确出口的技术债,
写入风险表, 不阻塞当前开发。

### 1.4 音频事件层 (恢复被规格遗漏的一层)

| 组件 | License | 结论 |
|---|---|---|
| **YAMNet 预训练模型** (AudioSet 521 类) | Apache-2.0 (TF Hub / tensorflow/models) | 可进产品, 权重几 MB, 直接推理不训练 |
| Silero VAD | MIT | 可进产品, 服务端二道 VAD |
| 音量+频带规则 (自写) | 自有 | 可进产品, MVP 首选("刺啦/笃笃"先用规则) |

### 1.5 语音层

| 组件 | License/条款 | 结论 |
|---|---|---|
| Gemini Live API (`gemini-3.1-flash-live-preview` 等) | 商业 API, 全部 Preview 状态 (models 页 2026-07-21) | 可用但有漂移风险; Voice Orchestrator 写成薄适配层, 模型名/参数配置化 |
| 降级备胎: faster-whisper (ASR) | MIT | 可进产品 |
| 降级备胎: Piper (TTS) | MIT | 可进产品 |

### 1.6 VLM 层

| 组件 | 条款 | 结论 |
|---|---|---|
| Gemini Flash 系列 (structured output) | 商业 API | 可用; 请求带 decision_id/step_id/context_version, 后端校验 |
| 备胎: GLM-4.6V-Flash | MIT (模型开源) | 可进产品/可自部署, 隐私与成本备选 |

### 1.7 存储层

| 组件 | License | 结论 |
|---|---|---|
| Supabase (托管或自部署) | Apache-2.0 | 可进产品 |
| Postgres + pgvector | PostgreSQL License / PostgreSQL-style | 可进产品 |

## 2. 数据与知识源计划 (只列可直接进产品的)

### 2.1 P0: 立即获取

| 资源 | License | 用途 | 动作 |
|---|---|---|---|
| **自采第一人称视频** (见 §4) | 自有, 唯一可商用训练的视频数据 | 阈值调参、回归测试、VLM fixture、未来训练集 | 用户本人录制, 存 `data/test_videos/` |
| **HowToCook** (Anduin2017/HowToCook) | **Unlicense (公有领域)** | "菜谱→SOP JSON"解析器的测试与生产语料; 结构统一的中文 Markdown(原料/计量/步骤分节) | `git clone`, 抽 10 道菜先做 SOP 解析 fixture |
| **MediaPipe hand_landmarker.task** | Apache-2.0 | 已在 `weights/` | 已有 |
| **YAMNet 权重** | Apache-2.0 | 音频事件层 | TF Hub 下载, 几 MB |

### 2.2 P1: 注册免费 key / 留配置位

| 资源 | 条款 | 用途 | 动作 |
|---|---|---|---|
| **USDA FoodData Central API** | 美国政府数据, 公有领域; 免费 key | 受控 RAG 的营养/替换食材可信来源 | api.data.gov 注册免费 key |
| **USDA/FSIS 安全温度表** | 公有领域 | 固化为确定性食品安全规则(spec §12.3), 不走检索 | 人工整理进 `sop/safety_rules.json` |
| **Open Food Facts API** | ODbL (数据), 可商用但需署名+同等共享 | 条码/品牌识别(注意中国商品覆盖弱) | 只留 API 配置位, 不下 dump |
| **TheMealDB API** | 免费, 需署名 | 西餐菜谱补充, 低优先 | 留配置位 |

### 2.3 明确不进产品管线的

- 一切 NC / research-only 数据集(见附录 A): 只允许内部离线评估, 产出仅限
  "阈值改多少、启发式换不换"这类决策, 不得混入训练数据或产品资产。
- XiaChuFang 语料(爬取数据, 权属不清): 仅内部 RAG 检索实验, 商用前必须替换为
  自建或已授权菜谱库。HowToCook + 自建菜谱是干净的生产路线。
- 100DOH / VISOR / EPIC 系列 / CaptainCook4D / HoloAssist: 附录 A。

## 3. 仓库目标结构

```text
NomaChef/
├── firmware/            # ESP32 (Phase B 起)
├── server/
│   ├── gateway/         # 设备接入 + Live 代理 (Session Server)
│   ├── perception/      # 现 perception/ 迁入或保持顶层, 接口不变
│   ├── engine/          # State Engine + SessionContext (单写者)
│   ├── events/          # 事件信封、event log、回放器
│   ├── voice/           # Gemini Live 适配层 (薄)
│   ├── vlm/             # 关键帧确认 + schema 校验
│   └── knowledge/       # SOP 冻结、受控 RAG、安全规则
├── sop/                 # SOP JSON schema + 手写菜谱 (README 已承诺, 待建)
├── harness/             # 回放与 live 调试入口
├── tests/
├── data/                # 自采数据 (gitignore 大文件)
└── docs/
```

迁移原则: `perception/` 的"ndarray 进、dataclass 出"契约不动; 目录迁移放在
Runbook Step 1 事件信封改造之后, 避免同时动接口和位置。

## 4. 为什么必须自己录固定测试视频和自采数据

这是整个数据计划里唯一的 P0, 原因有六个, 都直接对应架构验收项:

1. **离线调参是 10 倍速开发**: `harness/live_perception.py --source video.mp4`
   已支持回放。grip 阈值、去抖帧数、词表、VLM prompt 全部可以对着同一段录像
   反复调, 不用每次真开火。CLAUDE.md §10 第 1 条本来就是这个设计。
2. **确定性回放验收需要固定输入**: spec 15.3 要求"状态可通过事件 JSONL 确定性
   回放"。没有固定视频, 回归测试没有基准: 改一行融合代码后无法证明行为没变。
   固定视频 + 固定代码版本 = 可 diff 的事件流。
3. **机位和镜头仍需自采校准**: 机位已定为头戴式第一人称(俯视工作区), 与
   EPIC/Ego4D 同类视角, 公开数据的参考价值上升; 但 160° 广角畸变、DFR1154 的
   分辨率/画质、以及具体俯角组合仍是独有的, 阈值必须在自己的画面上校准。
4. **中国厨房物品在公开数据里缺席**: 深色酱油瓶/老抽/蚝油瓶、炒锅锅型、
   中式调料包装, 公开数据集覆盖近零。YOLO-World 开放词表也需要用你的实拍帧
   验证"soy_sauce_bottle"这个词到底框不框得住你家那瓶。
5. **VLM fixture 必须稳定**: spec 15.3 要求"固定 20-30 张关键帧, 相同输入多次
   调用关键字段稳定"。fixture 一旦选定就不能变, 必须是自有版权图像才能长期
   入库、入 CI、未来给标注团队。
6. **这是唯一 license 干净的训练资产**: 附录 A 里所有视频数据集都不可商用训练。
   自采视频(经用户授权)是 spec §9 "step_events 就是具身智能数据集雏形"这条
   产品叙事的唯一合法起点。录制矩阵(正常光/背光/蒸汽/油烟/空手/戴手套/不同
   瓶型)见 spec §9.2。

一句话: 公开数据集回答"模型一般行不行", 自采数据回答"在我的机位、我的厨房、
我的酱油瓶上行不行"。产品验收只关心后者。

## 5. License 风险汇总表

| 风险 | 等级 | 出口 |
|---|---|---|
| ultralytics AGPL-3.0 | 高(商业化前) | Enterprise License / 换 Grounding DINO(Apache) / 后端开源, 三选一; demo 期不阻塞 |
| Gemini Live 全 Preview + Interactions API 迁移信号 | 中 | 薄适配层 + faster-whisper/Piper 降级链路 |
| ESP-SR 限 Espressif SoC | 低(本来就跑在 ESP32 上) | 落地前读 LICENSE 原文确认再分发条款 |
| Open Food Facts ODbL 同等共享 | 低 | 只调 API 展示, 不把其数据混入自有数据库再分发 |
| XiaChuFang 爬取语料 | 中(若误入生产) | 严格限内部实验, 生产用 HowToCook + 自建 |

## 附录 A: 仅限内部离线评估的资源 (永不进产品)

| 资源 | 许可性质 | 内部评估用途 | 是否下载 |
|---|---|---|---|
| CaptainCook4D | 研究用途(下载前核对官网条款) | 用带错误标注的做菜视频测状态机误推进率 | 可选, 标注+10-20 段子集 |
| EPIC-SOUNDS 标注 | 研究(EPIC 系 CC BY-NC 4.0) | 验证厨房声音事件规则/YAMNet 召回 | 标注 clone(MB 级), 音频暂缓 |
| HoloAssist | 研究(下载前核对) | 只读 transcript: 真人教练何时开口/怎么纠错, 喂语音策略设计 | 仅标注/文本 |
| VISOR | CC BY-NC 4.0 | 手物关系失败案例补充分析 | 暂不下载, 只注册 |
| 100DOH 预训练模型 | 研究 | 与 MediaPipe 启发式对比评测 | 可推迟 |
| Ego-HOIBench | 研究(2025) | 同上, 观察名单 | 暂不下载 |
| XiaChuFang Corpus | 爬取, 权属不清 | RAG 检索实验(子集) | P1, 仅子集 |

规则: 附录 A 的任何字节不得出现在训练管线、产品资产或对外交付物中;
每次下载在本文件追加一行记录: 日期、版本、许可条款原文链接、存放路径。
