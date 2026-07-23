# Phase 1 Guideline — 本地感知层(Layer 1)

> 目标:不碰云端、不碰硬件,在笔记本上把 CLAUDE.md §2 的"第一层连续感知"跑成一个
> 可交互、可产数据的闭环。这一层无论后面架构怎么变都必须存在,所以最先做。

---

## 1. 本阶段做什么 / 不做什么

**做:**
1. YOLO-World 开放词汇检测:按 SOP 步骤的 `objects_involved` 动态设词表,逐帧框物体。
2. MediaPipe Hands:21 关节点,判断手在哪、是否张开/捏合。
3. 基础交互信号(fusion):`hand_near_object` / `hand_holding_object`,纯几何判定。
4. 数据落盘:每个 live session 写一份 JSONL 事件流到 `data/sessions/`,
   格式对齐未来 Supabase 的 `step_events` 表,这就是具身智能数据集的雏形。
5. 实时可视化 harness:webcam 循环,检测框 + 手关键点 + 当前交互状态叠加显示。

**不做(后续 Phase):**
- 云端 VLM 巡检(Layer 2)、Gemini Live 语音(Layer 3)
- 状态引擎打分推进(engine/,Phase 2)
- Pi 采集推流、Supabase、Next.js 遥控页

## 2. 目录结构(本阶段涉及)

```
SousAI/
├── perception/               # Layer 1 核心,全部可独立 import
│   ├── detector.py           #   YOLO-World 封装:set_vocab(step) → detect(frame) → [Detection]
│   ├── hands.py              #   MediaPipe Hands 封装:detect(frame) → [HandState]
│   ├── fusion.py             #   纯函数几何判定:交互信号 + InteractionEvent
│   └── session_logger.py     #   JSONL 事件落盘(step_events 雏形)
├── harness/                  # 可执行入口,只做编排不放逻辑
│   ├── smoke_yolo_world.py   #   静态图/视频 smoke(已有)
│   └── live_perception.py    #   webcam 实时闭环(本阶段主入口)
├── tests/
│   └── test_fusion.py        #   fusion 几何逻辑单测(唯一值得单测的部分)
├── data/
│   ├── test_frames/          # 静态测试帧
│   ├── test_videos/          # 第一人称做菜录像(最高杠杆的测试集,尽快录!)
│   └── sessions/             # live 跑出来的 JSONL 事件流(git ignore)
└── weights/                  # 模型权重(git ignore)
```

**规矩:perception/ 里的模块不 import cv2 的 GUI、不碰摄像头,只吃 ndarray 吐 dataclass。**
摄像头、窗口、键盘全在 harness 里。这样同一套 perception 以后直接换成 Pi 推流帧也不用改。

## 3. 运行方式

```bash
cd ~/Documents/SousAI

# 静态帧 smoke(不需要摄像头权限)
.venv/bin/python harness/smoke_yolo_world.py data/test_frames/xxx.jpg

# 实时感知(主入口;终端首次运行会弹摄像头权限)
.venv/bin/python harness/live_perception.py            # 默认摄像头 0
.venv/bin/python harness/live_perception.py --source data/test_videos/cook1.mp4  # 用录像回放
# 按 q 退出;退出时 session JSONL 路径会打印出来

# 单测
.venv/bin/python -m pytest tests/ -q
```

## 4. 关键设计决定(以及为什么)

| 决定 | 理由 |
|---|---|
| 词表按"步骤"切换,不用全菜谱大词表 | 词表越小误检越少;SOP 的 `objects_involved` 就是接口 |
| YOLO-World 跑 MPS,MediaPipe 跑 CPU | MediaPipe legacy API 本来就是 CPU 优化的,两者并行互不抢 |
| 检测不必每帧跑,手可以每帧跑 | YOLO ~几十ms/帧,手部 ~5ms;检测隔 N 帧跑一次,中间沿用上次框(Phase 1 先这样,ROI tracker 留到 Phase 2) |
| `holding` 用"指尖收拢 + 手框与物框 IoU/包含"近似 | 免训练。单信号必误判,但 Phase 2 状态引擎会把它当 0.4 权重证据而不是真值 |
| 事件去抖:状态连续 K 帧一致才发 event | 检测抖动会造成 near→holding→near 高频翻转,污染数据 |
| JSONL 每行一个事件,带 wall-clock + 帧号 | 直接对齐 `step_events` 表;以后回放录像可离线重新生成 |

## 5. 数据格式(session JSONL)

`data/sessions/2026-07-22T23-40-00_session.jsonl`,每行一个 JSON:

```json
{"t": 1784918400.123, "frame": 512, "type": "interaction",
 "event": "hand_holding_object", "hand": "Right", "object": "soy sauce bottle",
 "conf": 0.62, "hand_box": [412,220,540,388], "obj_box": [430,180,520,400]}
```

事件类型:
- `session_start` / `session_end`:元信息(词表、来源、分辨率)
- `interaction`:`hand_near_object` / `hand_holding_object` 及其 `_end` 变体
- `snapshot`(每 5 秒):当帧全部检测结果,用于事后复盘和调阈值

## 6. 验收标准(过了才算 Phase 1 完成)

1. `pytest tests/ -q` 全绿。
2. 静态厨房帧上,YOLO-World 能框出词表内出现的物体(conf ≥ 0.15)。
3. live harness 在笔记本上 ≥ 10 FPS 端到端(检测隔帧跑)。
4. 手里拿一个词表内物体(如瓶子/碗)在摄像头前晃,终端能打出
   `hand_holding_object` 且放下后打出 `_end`,JSONL 里有对应行。
5. 跑一次 2 分钟 session,JSONL 非空且能被 `json.loads` 逐行读回。

## 7. 与后续 Phase 的接口

- **状态引擎(Phase 2)**:订阅 `InteractionEvent` 流 + 周期 snapshot,按 CLAUDE.md §4
  打分表累积证据。fusion 的输出 dataclass 就是它的输入,不要绕过。
- **Layer 2 VLM 巡检**:live harness 里已有"当前帧 + 检测框"的合成图,直接作为 VLM 输入。
- **Pi 采集**:把 harness 的 `cv2.VideoCapture(0)` 换成 MJPEG URL 即可,perception 零改动。
- **录像回放**:`--source` 吃视频文件,意味着录一段第一人称做菜视频后,
  所有阈值调参都能离线做(CLAUDE.md §10 第 1 条,10 倍速调试)。

## 8. 已知坑

- mediapipe ≥ 0.10.30 已**移除** legacy `mp.solutions` API,只能用 Tasks API
  (`HandLandmarker`,VIDEO 模式要求时间戳单调递增,由 HandTracker 内部维护)。
  模型文件在 `weights/hand_landmarker.task`(Google 官方 CDN 下载,~7.5MB)。
- macOS 上 OpenCV 窗口必须在主线程;不要把 `cv2.imshow` 挪进线程。
- 终端(iTerm/Terminal)首次开摄像头会弹系统权限,拒绝过一次就要去
  系统设置 → 隐私与安全 → 摄像头手动开。
- YOLO-World 的文本编码器是英文 CLIP:**SOP 的 `objects_involved` 必须存英文**。
