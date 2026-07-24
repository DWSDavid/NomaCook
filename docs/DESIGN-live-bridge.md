# Design — Gemini Live 接入主管线(live bridge)

> 现状:Gemini Live 只存在于 `harness/live_gemini_smoke.py` 和
> `server/voice/live_scene_demo.py` 两个孤立 demo,和状态引擎零交互。
> 管线里的 `voice.user_confirmation` 全部来自 scripted 假事件。
> 这是"感觉没有 Gemini 介入"的根因之一(另一个是 `--vlm` 默认 off)。

## 目标

把 Live 变成引擎的一个**事件源 + 播报出口**,三个能力分三期落地:

## L1 · Live 当嘴(播报出口)

实时 harness 里,narration item(intro/preview/transition/question/complete)
不再走离线 TTS,改为发文本给 Live 会话让它说出来。
- 实现:`server/voice/live_bridge.py`,维护一个 asyncio 队列;
  引擎线程 push 文本,Live 协程 `send_realtime_input(text=...)`。
- 好处:同一把嗓子既播步骤又答问题,听感统一;天然可打断。

## L2 · Live 当耳(确认事件源)

用户说"好了/切完了/下一步",Live 的输入转写 → 匹配当前
`pending_question` 或当前步骤 → 生成真实的 `voice.user_confirmation`
EventEnvelope(权重 0.4-0.6,见 SOP)喂给 `engine.consume()`。
- 匹配规则先用关键词表(好了/完成/OK/下一步/切好了),不上 NLU。
- 误听保护:确认类事件要求转写置信度 + 关键词双命中;
  听不清就让引擎维持原状态,宁可追问不可误推进。

## L3 · Live 当眼(带画面的即问即答)

用户问"这样切行吗",Live 已持有摄像头流(smoke 里已验证),
bridge 把"当前步骤 + SOP 上下文"注入,让回答有步骤感知。

## 重连(demo 现场最易炸点,CLAUDE.md §2 早有预警)

- Live 单会话有时长上限,做菜必超。bridge 必须把状态所有权留在引擎侧:
  重连时把"当前第几步 + 该步 instruction + pending question"作为
  新会话的初始上下文注入,用户无感。
- 心跳检测:收流中断 > 3s 即触发重建,重建期间播报退化为本地 `say`。

## 不做的

- 不让 Live 决定步骤推进(它只是证据源之一,决定权在打分引擎)。
- 不做开放式"用户在干嘛"提问(维持封闭问题原则)。

## 实施顺序建议

L1(半天,纯管道)→ L2(一天,含误听测试)→ L3(半天)。
先在 `--source` 录像回放模式下开发 L1/L2(Live 只收合成音频/文本),
真机联调放最后。
