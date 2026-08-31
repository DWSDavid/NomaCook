# 设计 — NomaChef 实时服务(camera-driven Live)

> 2026-07-24。目标:把现在跑视频文件的离线管线,变成 ESP32 摄像头驱动的实时服务,
> 给后端和产品端用。离线基线已 tag 为 `offline-base-v1`,随时可回退。
> 上位依据:`docs/NOMACHEF-TECHNICAL-SPEC.md`(ESP32 只采集推流,不跑 AI;
> Device Gateway 用 FastAPI + WebSocket;推理放独立 worker,别塞 socket 线程)。

## 核心思路:三个接缝,一个大脑

现在 `harness/run_pipeline.py` 是一个 460 行的大函数,把"读帧、跑感知、打分、
出语音、写文件"全焊死在一起。实时化不是重写这坨,是把它拆成三个能替换的接缝,
中间那个"大脑"离线实时共用一份。

```
┌─────────────┐   (pts_ms, frame)   ┌──────────────┐   状态/旁白事件   ┌────────────┐
│ FrameSource │ ──────────────────▶ │ SessionCore  │ ────────────────▶ │ OutputSink │
└─────────────┘                     │  (那个大脑)   │                   └────────────┘
  离线:视频文件                       │ 感知+打分引擎  │                     离线:写JSONL+渲染视频
  实时:ESP32 MJPEG流                  └──────────────┘                     实时:WebSocket推给产品端+TTS下发设备
```

- **FrameSource**(已完成,`server/live/frame_source.py`):唯一区分离线/实时的地方。
  `VideoFileSource`(确定性时间戳)和 `CameraStreamSource`(墙钟 + 断线重连)都吐
  `(pts_ms, bgr_frame)`。下游一行不用改。
- **SessionCore**(待 Codex 从 run_pipeline 抽出):吃帧 → YOLO/手/HSV/Gemini VLM →
  EventEnvelope → StateEngine → 吐状态快照和旁白提示。不认来源、不认出口。这是复用的资产。
- **OutputSink**(待抽象):结果去哪。离线写文件 + 渲染视频;实时推 WebSocket + 下发 TTS。

## Device Gateway(设备和产品端的接口)

一个 FastAPI 服务,两条 WebSocket 通道,一个会话:

```
DFRobot ESP32 ──JPEG帧──▶ /ws/device ──┐
              ◀─PCM音频──            │
              ◀─控制JSON─            │
                                   [Gateway] ── SessionCore（独立worker线程/进程）
                                     │
产品前端     ──控制JSON──▶ /ws/app ──┘
（网页/App）  ◀─状态事件──   （当前步骤、分数、检测框、旁白文本、成品）
```

### 设备侧 `/ws/device`(对 ESP32)
- 上行:JPEG 帧(MJPEG 或 WS JPEG,640x480,5-10 FPS)、PCM 麦克风音频(16kHz)、心跳。
- 下行:PCM 语音(播报,喂扬声器 + 给 AEC 做参考)、控制命令。
- 最短集成路径:ESP32 的 CameraWebServer 直接暴露一个 MJPEG URL,
  Gateway 用 `CameraStreamSource(url)` 读,不用等固件写 WS 推流。先用这个跑通。

### 产品侧 `/ws/app`(对网页/App)
- 上行:开始/暂停、选菜谱下发 SOP、用户手动"这步好了"(直接推进的兜底信号)。
- 下行:状态事件流,复用现有 `EventEnvelope` + 状态快照(当前第几步、分数、
  检测结果、该说的旁白、成品)。这一份数据既驱动产品 UI,也是便携屏画面,一套两用。

## 实时循环:推理别卡在 socket 线程上(技术方案硬要求)

```
帧到达(socket线程) ─▶ 只做一件事:塞进 latest_frame 槽(丢旧留新)
                                          │
推理worker(独立线程) ─▶ 循环取 latest_frame ─▶ SessionCore.step(pts, frame) ─▶ 广播事件
```

- socket 线程只收帧、更新"最新一帧"槽,永不阻塞。丢帧是对的:实时系统宁可看最新的,
  不要排队积压。
- 推理 worker 按自己的节奏(检测每 N 帧、Gemini 每 5 秒)消费最新帧,产出事件,
  广播给所有 `/ws/app` 订阅者。
- SessionCore 内部状态就是 StateEngine,事件仍写 append-only JSONL,实时也能事后重放。

## 断线与状态归属(demo 最易炸的点,CLAUDE.md 早有预警)

- 状态所有权永远在 SessionCore,不在设备也不在 Live 会话。
- ESP32 掉线重连:`CameraStreamSource` 自带重连,会话状态不丢,重连即续。
- Gemini Live 会话超时(约10分钟):重连时把"当前第几步 + 该步 SOP + 待回答的问题"
  作为新会话初始上下文注入,用户无感。Live 只是嘴和耳,不掌管步骤推进。

## 与离线基线的关系

抽出 SessionCore 后,`run_pipeline.py` 退化成"VideoFileSource + SessionCore +
文件/视频 OutputSink"的薄壳。**好处:离线基线继续当同一个大脑的回归测试床**,
明天的新视频照跑,106 个测试继续绿。实时和离线永远是同一套逻辑,不会分叉。

## 跨服务边界(队友的 Go 后端)

产品端的会话/用户/菜谱管理在队友仓库 [ztboxs/NomaChef-Backend](https://github.com/ztboxs/NomaChef-Backend)(Go)。
我们这边只做实时媒体 + 感知 + 状态引擎。两边契约见其 `docs/integrations/session-server.md`,
`/ws/app` 的事件格式对接前要跟队友对一次,别各写各的。

## 落地顺序

1. **纯重构**:抽 SessionCore + OutputSink,run_pipeline 改用它们,行为不变,测试全绿。
   (见 `HANDOFF-live-step1.md`)
2. **最小实时**:FastAPI Gateway + `/ws/device` 读 MJPEG + `/ws/app` 推状态,先用 Mac
   摄像头或 ESP32 的 MJPEG URL 跑通,终端能看到实时步骤推进。
3. **音频闭环**:麦克风上行(用户说"好了")+ TTS 下行(讯飞,已通)。
4. **Gemini Live 接入**:实时对话作为额外的耳/嘴,重连注入上下文。

一次做一步,每步 tag 一个可回退版本。先 1,别跳。
