# AI-2026-005 Evidence

## Manager Activation

- Human owner approved design, implementation plan and `CONTRACT-AI-REALTIME-001 v1`.
- Management/Contract revision: `6d5d088d57e558277c858926b8497140151d9f85`.
- AI base: `d8e68742d1cc3730dc19f097ad90a2d585b2e40a`.
- Activation changed management files only; no private config was read and no model was called.

Executor must append RED/GREEN commands, exit codes, test counts, dependency changes, privacy audit and full implementation/evidence SHAs.

## Executor Evidence — 2026-08-29

### Baseline and privacy

- Task branch：`codex/ai-realtime-model-service-v1`。
- Opening HEAD：`d44c6a3ac7668d50b8b40bbd6898d0622a30c44e`；task base
  `d8e68742d1cc3730dc19f097ad90a2d585b2e40a` 为祖先。
- Management/Contract revision：`6d5d088d57e558277c858926b8497140151d9f85`。
- 开工前及最终工作区均仅有 `?? .gitkeep`、`?? config.yaml`；两个文件正文未读取，且未被修改、移动、暂存、提交、忽略或删除。
- 根管理仓库既有未跟踪 `.cursor/` 和 `management/feature-panorama-2026-08-26-v1.md`；未读取、未修改。

### RED → GREEN

- RED：`./.venv/bin/python -m pytest tests/realtime -q`，exit `2`，缺少
  `server.realtime` 和 `server.gateway.realtime_app`。
- GREEN：`./.venv/bin/python -m pytest tests/realtime -q`，exit `0`，`19 passed / 0 skipped`。
- Regression：`./.venv/bin/python -m pytest tests/model_service -q`，exit `0`，`66 passed / 0 skipped`。
- Compile：`./.venv/bin/python -m compileall -q server/realtime server/gateway`，exit `0`。
- `git diff --check`：exit `0`。
- FastAPI TestClient 产生一项既有 Starlette/httpx deprecation warning；无测试失败。

### Delivered boundary

- `server/realtime/contracts.py`：严格 control envelope、UUID/generation/producer/sequence、RFC3339 时间、context/announce/lifecycle 校验和 12-byte Opus binary frame。
- `server/realtime/codec.py`：PyAV `libopus` 实际编解码与 PCM16k/24k、Opus48k stereo 配置；20ms roundtrip 通过。
- `server/realtime/provider.py`：transport-neutral `RealtimeProvider`、ProviderEvent 和显式配置 Qwen WebSocket Provider；无本机音频设备依赖。
- `server/realtime/session.py`：单 `session_id+generation`、semantic VAD 事件、assistant text/audio、interrupt、announce 幂等、pause/resume/stop、stale/乱序、1 秒有界队列与 BACKPRESSURE。
- `server/gateway/realtime_app.py`：内部 Bearer-authenticated WebSocket、subprotocol、health/readiness、owner conflict/capacity 和断开清理。
- 生产路径未打开本机 microphone/speaker，未写 session/PCM/Opus 文件，未执行 NomaCook Tool 或 Backend 状态。

### Commit and boundary

- Implementation/test Commit：`e12188b3817373e3effdc71703364d20b9f15372`。
- 未调用真实 Qwen；Fake Provider/ASGI client only；未启动 Backend、Node、App、Hardware；未部署、push 或 merge。
- 当前状态：`machine-complete / manager-review-pending / integration-pending`。

## Manager Initial Review

- Activation/implementation ancestry and allowed-path Delta passed; reviewed
  Head is `1d43f615470a7431e95afad2c9ac807fcecd1fe0` and implementation is
  `e12188b3817373e3effdc71703364d20b9f15372`.
- Production route probe: `PRODUCTION_REALTIME_ROUTE_PRESENT=false`.
- Announce probe: `ANNOUNCE_EVENTS_BEFORE_AUDIO=announce.completed`.
- Wire-order probe:
  `SEND_PENDING_ORDER=response.audio_started,response.audio_done,binary_audio`.
- Incremental audio probe: a valid 40 ms PCM fragment produced
  `session.failed:CODEC_UNAVAILABLE` and zero output frames.
- Official Qwen Server Events documentation confirms `session.updated` is the
  successful update acknowledgement and `response.audio.delta` is incremental
  PCM without a contract-fixed 20 ms event size.
- Manager did not read private files, modify production code, call a Provider,
  start services or repeat the executor suites.
- Result: `blocked-by-P0`; one closed Delta owns all three findings.
