# HANDOFF: Gemini Live 摄像头冒烟测试 (Track B / Step B1)

> 分工: Claude 已写好代码; 本文档给用户或 Codex 实机执行。
> 前置: 仅需 GEMINI_API_KEY。不依赖 Step 1-3, 不依赖硬件。

## 1. 一次性准备

```bash
cd ~/Documents/NomaChef
uv pip install -p .venv sounddevice
```

`.env` 里填入 key (参考 `docs/SETUP-KEYS.md`, `.env` 已被 gitignore):

```
GEMINI_API_KEY=<你的key>
```

可选覆盖模型名 (默认 `gemini-3.1-flash-live-preview`):

```
GEMINI_LIVE_MODEL=<模型名>
```

## 2. 运行

**戴上耳机** (否则扬声器出声进麦克风, 模型和自己对话; 没耳机就加 `--half-duplex`)。

```bash
.venv/bin/python harness/live_gemini_smoke.py --kickoff "你现在看到什么?"
```

首次运行 macOS 会弹摄像头 + 麦克风权限, 都允许。其他模式:

```bash
# 纯音频 (排查视频问题时用)
.venv/bin/python harness/live_gemini_smoke.py --no-video

# 不戴耳机
.venv/bin/python harness/live_gemini_smoke.py --half-duplex

# 更长会话 (注意 audio+video 无压缩上下文约 2 分钟, 默认 120s 就是这个原因)
.venv/bin/python harness/live_gemini_smoke.py --duration 300
```

## 3. 测试动作脚本 (约 2 分钟)

1. 连接后等 kickoff 的语音回复, 核对描述与画面是否一致。
2. 对镜头摆 2-3 样厨房物品 (碗/瓶/锅铲), 问"我手里拿的是什么?"
3. 模型说话说到一半时直接开口打断, 确认它停下来听你说 (barge-in)。
   `--half-duplex` 模式下跳过此项 (设计上不支持)。
4. 换个角度再问一次"现在画面里有什么变化?"

## 4. 验收清单 (结果填回 runbook Step B1)

- [ ] 连接成功, 能听到语音回复 (模型名可用; 404 见下面排错)
- [ ] 场景描述与摄像头画面一致 (视频帧确实进了模型)
- [ ] 说话可打断, 终端出现 `[barge-in]` 行
- [ ] 终端 `[你]` / `[诺妈]` transcript 与实际语音一致
- [ ] `data/sessions/*_gemini_live_smoke.jsonl` 完整 (session_start → connected
      → transcript/frames_sent → session_end)
- [ ] 记录: 实际用的模型名 / 首次语音回复延迟体感 / 打断响应体感

## 5. 排错

| 现象 | 处理 |
|---|---|
| 模型名 404 / not found | 在 [AI Studio 模型列表](https://ai.google.dev/gemini-api/docs/models) 查当前 Live 模型名, 用 `GEMINI_LIVE_MODEL` 覆盖。候选: `gemini-3.1-flash-live-preview`、`gemini-2.5-flash-native-audio-preview-12-2025`、`gemini-live-2.5-flash-preview` |
| `sounddevice` 导入失败 | 确认装在 `.venv`: `uv pip install -p .venv sounddevice` |
| 听不到声音 | 系统输出设备是否是耳机; 音量; 先跑 `--no-video` 减少变量 |
| 模型自问自答 | 没戴耳机。戴耳机或 `--half-duplex` |
| 摄像头打不开 | `--source 0` 是 Step 0 验证过的唯一设备; 关掉占用摄像头的其他 App |
| 会话 2 分钟左右被断 | 预期内 (audio+video 无压缩上下文上限)。这正是 spec §11.2 生产用 audio-only + VLM 分离的依据, 记录实测断开时间即可 |
