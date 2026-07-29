# NomaChef 同步快照 — 2026-07-24

## 稳定基线

- Git commit：`252dd9ba4275748eb42c4c8ae6c3daa480a14295`
- Git tag：`vlm_ctx_v1`
- 状态：`main` 与 `origin/main` 一致；工作区在接入讯飞前为 clean。
- GitHub：<https://github.com/DWSDavid/NomaChef/tree/vlm_ctx_v1>

该基线包含：VLM 接收清理后的手部关系与相对位置 context；系统提示第 7 条；固定 5 秒 VLM 判别；SOP 菜名收尾旁白；清爽检测框；`vlm_ctx_v1` 完整验收记录。

## 科大讯飞接入

新增的语音链路：

```text
NomaChef 中文旁白
  ├─ zh-CN ─────────────────────────────┐
  └─ 其他语言 → 讯飞机器翻译 ────────────┤
                                        ↓
                         讯飞 WebSocket 流式 TTS
                                        ↓
                     16 kHz PCM 边收边播 / 收集成 WAV
                                        ↓
                         viaim 或其他系统音频输出设备
```

实现范围：

- HMAC-SHA256 WebSocket 鉴权，不记录签名 URL 或密钥。
- Provider-neutral PCM 流与原子 WAV 写入。
- 讯飞机器翻译，支持独立翻译密钥或复用应用密钥。
- 离线成片增加 `--narrate iflytek --language ...`。
- 实时识别增加 `--speech-backend iflytek`，后台队列不阻塞视觉识别。
- 实时提示采用 latest-wins 队列，合成较慢时会丢弃尚未播出的旧提示，避免语音越积越晚。
- 音频缓存包含 provider、语言、发音人和风格版本，避免切换音色后误复用。
- 中文审计原文保留，译文写入 schedule 和 sidecar。

代码分支：<https://github.com/DWSDavid/NomaChef/tree/agent/iflytek-voice>
草稿 PR：<https://github.com/DWSDavid/NomaChef/pull/1>

全套验收 `106 passed`，覆盖鉴权固定向量、翻译签名与解析、PCM 分片、WAV
原子写入、重试边界、缓存隔离和命令行预检。真实账号联网验收已通过：默认发音人
`x4_yezi`（小露），语速 58、音量 44、音高 46；本地 `.env` 已配置且保持 Git 忽略，
凭证未进入代码、文档或提交历史。

## 完整视频回归

已用 `NC_AIV_FHF.mov` 跑完视觉主链路：

- 5,455 帧、1,808 个事件、7 次步骤切换，最终状态 `completed`。
- 第 7 步于 180.2 秒完成，结尾菜名与旁白正确。
- Gemini 固定每 5 秒判别，清理后的手部关系与相对位置 context 正常生效。
- 视觉输出：`data/sessions/ses_rv_tomato_egg_7step_1_nc_aiv_fhf/run_iflytek_voice_regression_v1/annotated.mp4`。
- 小露旁白成片：复用已验收的视觉 run，再调用与 runner `--narrate iflytek` 相同的 `narrate_run`，输出 `data/sessions/ses_rv_tomato_egg_7step_1_nc_aiv_fhf/run_iflytek_voice_yezi_v1/annotated_narrated.mp4`，没有重复运行或改变 VLM 判定结果。
- 讯飞实际合成 24 条旁白，排期选中 16 条；成片 181.833 秒、1280×720，视频 MPEG-4 + AAC 16 kHz 单声道音轨，已通过 `ffprobe` 与 SHA-256 复制一致性检查。
- 成片 SHA-256：`aa6c3a1fd7ac55e0a85db329839c80c7d0a02e0e5c7055ffe176cdbfbdca51c1`。

## 外部条件与已知边界

- 新环境仍需在本地 `.env` 填写有效的讯飞 `APPID`、`APIKey`、`APISecret`，并在控制台开通在线语音合成与目标发音人。
- 当前应用的中文 `x4_yezi` 已授权并实测通过；机器翻译仍返回业务码 `11200`，英文及其他翻译旁白需先为当前 APPID 开通机器翻译，或配置独立 `IFLYTEK_MT_*` 凭证。
- viaim 比赛专用 Skill SDK/输入输出契约；公开资料尚不足以确认第三方 PCM 是否能直送耳机。
