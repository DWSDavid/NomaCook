# Gemini Live 本地音视频整合测试

这个入口把本机摄像头、麦克风和扬声器接到 Gemini Live，用于在真实硬件到货前验证：

```text
摄像头 JPEG（最多 1 FPS） ─┐
                            ├─> Gemini Live ─> 24 kHz 语音 + 终端转写
麦克风 16 kHz PCM ─────────┘
```

它不会调用或修改本地 YOLO/MediaPipe 感知代码。画面和麦克风音频会发送给 Gemini API；运行前请移开含隐私、账号或其他敏感信息的物品。

## 配置

在仓库根目录创建本地 `.env`（已被 Git 忽略），填入已有 key：

```dotenv
GEMINI_API_KEY=你的现有key
```

不要把真实 key 写进 `.env.example`、源码、截图、终端命令或聊天。

## 运行

建议戴耳机，避免 Gemini 的扬声器输出重新进入麦克风：

```bash
cd ~/Documents/NomaChef
.venv/bin/python -m server.voice.live_scene_demo
```

启动后程序会发送一次中文场景描述请求；随后可以直接对麦克风提问，例如：

- “你现在看到了什么？”
- “画面里有哪些厨房用品？”
- “我的手里拿着什么？”
- “画面中有没有明显的安全风险？”

按摄像头窗口中的 `q` 或终端中的 `Ctrl-C` 退出。默认 100 秒自动结束，因为 Gemini Live 的音频+视频单连接有时长限制。

## 常用选项

```bash
# 查看本机音频设备编号
.venv/bin/python -m server.voice.live_scene_demo --list-audio-devices

# 指定摄像头和音频设备
.venv/bin/python -m server.voice.live_scene_demo \
  --source 0 --input-device 2 --output-device 3

# 只验证画面描述和终端转写，不采集麦克风、不播放声音
.venv/bin/python -m server.voice.live_scene_demo \
  --no-microphone --no-speaker --no-display --duration 20
```

视频帧发送间隔不能低于 1 秒。模型名默认使用 `gemini-3.1-flash-live-preview`，也可通过 `GEMINI_LIVE_MODEL` 或 `--model` 覆盖，方便 Preview 模型迁移。

## 科大讯飞流式播报

讯飞接入与 Gemini Live 相互独立：NomaChef 生成短句后，将整句文本上传到讯飞在线 TTS，返回的 16 kHz PCM 音频会边接收边播放。非中文模式先调用讯飞机器翻译，再选用控制台已授权的对应语言发音人。

配置方法见 [API Key 配置指引](../../docs/SETUP-KEYS.md)。实时识别演示：

```bash
.venv/bin/python harness/iflytek_tts_smoke.py \
  --language zh-CN \
  --iflytek-speed 58 \
  --iflytek-volume 44 \
  --iflytek-pitch 46 \
  --play

.venv/bin/python harness/live_recognition_demo.py \
  --speech-backend iflytek \
  --language zh-CN \
  --iflytek-speed 58 \
  --iflytek-volume 44 \
  --iflytek-pitch 46
```

`--iflytek-speed`、`--iflytek-volume`、`--iflytek-pitch` 的有效范围都是
0–100，默认均为 50。三个讯飞入口使用同一组参数名；建议先用一句话 smoke
试听，再把满意的数值用于实时识别或完整录像配音。

连接 viaim 或其他蓝牙耳机后，可以让程序使用系统默认输出设备，也可以指定设备编号或名称：

```bash
.venv/bin/python harness/live_recognition_demo.py \
  --speech-backend iflytek \
  --language en-US \
  --output-device 3
```

完整录像的离线配音仍在处理结束后生成，但每一句也使用同一流式接口收集成 WAV：

```bash
.venv/bin/python harness/run_pipeline.py \
  --source /absolute/path/to/cooking-demo.mp4 \
  --device mps \
  --vlm gemini \
  --narrate iflytek \
  --language zh-CN \
  --iflytek-voice x4_yezi \
  --iflytek-speed 58 \
  --iflytek-volume 44 \
  --iflytek-pitch 46 \
  --run-tag iflytek_zh_v1
```

`narration.json` 始终保留中文审计原文；实际译文、语言和发音人写入 `narration_schedule.json` 及每段音频的 sidecar 元数据。目标语种必须同时具备机器翻译和 TTS 发音人授权。
三个声音参数也会写入 schedule 和 sidecar；参数改变时，对应音频片段会自动重新合成，不会误用旧缓存。

英文或其他语言属于可选路径：将 `--language` 与 `--iflytek-voice` 改成控制台
已授权的目标语种，并先为当前 APPID 开通机器翻译，或配置独立的
`IFLYTEK_MT_*` 凭证。机器翻译未授权时，中文路径仍可独立正常运行。
