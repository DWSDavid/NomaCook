# AI-2026-005 Evidence

## 2026-08-30 v1.1 activation

- Management revision: `7dba37cd0e732011b86bfd797f4663c37bb2a185`.
- Contract/design/plan activation: `1249c2e0b6b0d7e367aaec89402c9043b904a044`.
- No v1.1 tests or Provider calls have run yet; prior v1.0 evidence remains historical only.

## Manager Activation

- Human owner approved design, implementation plan and `CONTRACT-AI-REALTIME-001 v1`.
- Management/Contract revision: `6d5d088d57e558277c858926b8497140151d9f85`.
- AI base: `d8e68742d1cc3730dc19f097ad90a2d585b2e40a`.
- Activation changed management files only; no private config was read and no model was called.

Executor must append RED/GREEN commands, exit codes, test counts, dependency changes, privacy audit and full implementation/evidence SHAs.

## Closed P0 Delta Evidence — 2026-08-29

### Authority and scope

- Branch：`codex/ai-realtime-model-service-v1`。
- Opening HEAD：`5def6fdf39e234b579852061d076944b96b8dd57`；reviewed implementation
  `1d43f615470a7431e95afad2c9ac807fcecd1fe0` 为祖先；任务基线
  `d8e68742d1cc3730dc19f097ad90a2d585b2e40a` 仍在祖先链。
- 未修改 `task.yaml`、`review.yaml` 或 `CONTRACT-AI-REALTIME-001`；未读取、修改、移动、暂存、提交、忽略或删除 `config.yaml`、`.gitkeep`。
- Delta 仅修改 `server/gateway/**`、`server/realtime/**`、`tests/realtime/**`。

### Root-cause RED

- 生产入口：`pytest tests/realtime/test_app.py -q` 在修复前 `2 failed / 2 passed`，因
  `server.gateway.main` 没有生产 `create_production_app`，Realtime route 未注册。
- Realtime session：`pytest tests/realtime/test_app.py tests/realtime/test_session.py -q` 在修复前
  `3 failed / 8 passed`，复现 announce 提前完成、40ms PCM/response_done 未覆盖以及新增事件类型缺失。
- 已用 Fake Provider/no-write probes 确认根因：旧入口只构造 Agent Model app；session 直接按单个 PCM delta 编码并分离 drain events/audio；announce 在 Provider write 后立即 completed。

### GREEN and implementation

- `server/gateway/main.py` 现在构造单一生产 FastAPI app，保留已有 Agent Model route，同时注册 Realtime `/v1/realtime-sessions:stream`；未 ready 时在 Provider factory 前拒绝 WebSocket。
- Qwen provider 等待 matching `session.updated` 后才报告 ready；`response.done` 的 failed/incomplete status 映射为固定失败。
- Realtime session 累积任意 PCM delta，按 20ms PCM 帧编码；统一 outbound queue 保证
  `response.audio_started → binary output_opus → response.audio_done`。
- announce 绑定唯一 provider response，缓存 transcript/音频至终态；只有 transcript 与 Backend 授权文本完全一致且存在完整音频时才释放并发送 `announce.completed`，否则丢弃并发送 `announce.failed`。
- Implementation/test Commit：`7c6e9ae29d80b7fb01c311d530cbac4c48863a1a`。

### Final verification

- `./.venv/bin/python -m pytest tests/realtime -q`：`29 passed / 0 skipped`，exit `0`。
- `./.venv/bin/python -m pytest tests/model_service -q`：`66 passed / 0 skipped`，exit `0`。
- `./.venv/bin/python -m compileall -q server/realtime server/gateway`：exit `0`。
- `git diff --check`：exit `0`。
- Production-path scan：未发现 `sounddevice`、`RawInputStream`、`RawOutputStream`、session file write；codec 的 `codec.open()` 仅为 PyAV codec context 初始化。
- Privacy/forbidden scan：无凭据、私钥、完整 token、config 读取或跨域模块匹配。
- 最终 `git status --short` 仅为 `?? .gitkeep`、`?? config.yaml`。
- 真实 Qwen Provider calls：`0`；未启动 Backend、Node、App、Hardware，未部署、push 或 merge。

状态：`machine-complete / manager-review-pending / integration-pending`。

## Manager Closed Delta Review

- Review base: `5def6fdf39e234b579852061d076944b96b8dd57`.
- Reviewed Head: `5c5d96a857567e96ea86a9fa6cdb45217cd5526f`.
- Implementation: `7c6e9ae29d80b7fb01c311d530cbac4c48863a1a`.
- P0-001/002/003 are closed by static review and accepted executor evidence.
- One new direct Delta P0: no-write factory probe returned
  `COMBINED_READY_STATUS=200` while `AGENT_MODEL_SERVICE_PRESENT=false`.
- Manager did not call a Provider, read private files, deploy or repeat suites.
- Result: one final readiness-only Delta remains.

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

## Final Readiness-only Delta Evidence — 2026-08-29

### Authority and scope

- Branch：`codex/ai-realtime-model-service-v1`。
- Opening HEAD：`2ca5605949b2ca07d97954fb78b53858c631c6d5`；management/contract
  revision：`6d5d088d57e558277c858926b8497140151d9f85`；opening HEAD is an
  ancestor of implementation/test commit `ae872c00c51a5f2252a616ab95aeee2340a9e3ce`。
- 仅修改 `server/gateway/main.py`、`tests/realtime/test_app.py` 及本节允许的
  脱敏证据文件；未修改 `task.yaml`、`review.yaml` 或契约。
- `config.yaml` 与 `.gitkeep` 仍保持未跟踪，正文未读取，且未被修改、移动、暂存、提交、
  忽略或删除。

### Root-cause RED

- `./.venv/bin/python -m pytest tests/realtime/test_app.py -q` 在修复前 exit `1`：
  `2 failed / 5 passed`；组合测试因生产入口缺少 Agent readiness 注入而无法执行，
  复现了组合 `/ready` 门禁缺口。

### GREEN and implementation

- `create_production_app` 现在组合 Agent Model 与 Realtime 状态，去重组件 `/health`、`/ready`
  后提供统一生产 `/health`、`/ready`；统一 readiness 同时检查 Agent settings/service、
  Realtime settings 和 codec。
- 新增的三组合测试固定 `503/503/200` 结果，并以 Fake Provider factory 断言 readiness
  路径 Provider calls `0`；测试同时固定 Agent Model `/v1/agent-model:stream` 与
  Realtime `/v1/realtime-sessions:stream` 均注册。
- Implementation/test Commit：`ae872c00c51a5f2252a616ab95aeee2340a9e3ce`。

### Final verification

- `./.venv/bin/python -m pytest tests/realtime/test_app.py -q`：`7 passed / 0 skipped`，exit `0`。
- `./.venv/bin/python -m pytest tests/realtime -q`：`31 passed / 0 skipped`，exit `0`。
- `./.venv/bin/python -m pytest tests/model_service -q`：`66 passed / 0 skipped`，exit `0`。
- `./.venv/bin/python -m compileall -q server/realtime server/gateway`：exit `0`。
- `git diff --check`：exit `0`。
- 生产路径扫描未发现 `sounddevice`、`RawInputStream`、`RawOutputStream` 或会话/音频文件写入；
  凭据/config 扫描未发现 Delta 文件中的私密配置或完整凭据。
- 真实 Provider calls：`0`；未启动 Backend、Node、App、Hardware，未部署、push 或 merge。

状态：`machine-complete / manager-final-delta-review-pending / integration-pending`。

## Realtime v1.1 Executor Evidence — 2026-08-30

### Authority and scope

- AI branch：`codex/ai-realtime-model-service-v1-1`；opening HEAD：
  `a3e662245497821671823359aadb9fdcaddecf6d`；task base：
  `6f9047ad779773e4ea6500b2ecc67c0805dc3ca0`；opening HEAD 为 base 后代。
- Management revision：`7dba37cd0e732011b86bfd797f4663c37bb2a185`；contract/design/plan
  revision：`1249c2e0b6b0d7e367aaec89402c9043b904a044`。
- 仅修改 `server/realtime/contracts.py`、`server/realtime/session.py`、
  `tests/realtime/test_app.py`、`tests/realtime/test_contracts.py`、`tests/realtime/test_session.py`
  及本任务允许的脱敏证据文件；未修改 `task.yaml`、`review.yaml`、契约或设计/计划。
- `config.yaml` 与 `.gitkeep` 仍保持未跟踪；正文未读取，且未被修改、移动、暂存、提交、忽略或删除。

### RED

- `./.venv/bin/python -m pytest tests/realtime -q`（实现前）exit `1`，`15 failed / 21 passed`；
  失败复现 v1.1 夹具与旧 schema `1.0` 不兼容，以及响应关联、partial PCM、announce 顺序和帧时间线缺口。

### GREEN and implementation

- `server/realtime/contracts.py` 固定 schema `1.1`，拒绝 schema `1.0` 和未知响应 payload 字段；响应事件
  严格校验 `utterance_id`、可空/回显 `message_ref` 与正数 `output_frame_count`。
- `server/realtime/session.py` 为普通 Provider response 生成临时 `utterance_id`，支持 text-first/audio-first；
  任意偶数字节 PCM 按 24 kHz mono 20 ms 累积，终态尾部静音补齐，空音频与异常终态 fail-closed。
- announce 缓存 transcript/audio，完成前不释放；仅授权文本逐字匹配且存在音频时按
  `assistant_text → audio_started → binary → audio_done → announce.completed` 输出，并回显
  Backend 的 `utterance_id`/`message_ref`；mismatch/no-audio/error/timeout 均 `announce.failed`。
- 输出 binary packet sequence 从 1、timestamp 从 0 起步并跨 response 连续递增（960）；AI control
  sequence 与 occurred_at 维持 Session 全局连续性。
- Implementation/test Commit：`502f5987467654108568a786c9af2209d9d19913`。

### Verification

- `./.venv/bin/python -m pytest tests/realtime -q`：`39 passed / 0 skipped`，exit `0`。
- `./.venv/bin/python -m pytest tests/model_service -q`：`66 passed / 0 skipped`，exit `0`。
- `./.venv/bin/python -m compileall -q server/realtime server/gateway`：exit `0`。
- `git diff --check`：exit `0`。
- Production/local-device scan：未发现本机麦克风/扬声器调用或会话/音频文件写入。
- Changed-file privacy scan：未发现私密配置正文、完整凭据、私钥或 config 文件内容；Fake Provider only，
  Provider calls `0`。未启动 Backend/Node/App/Hardware，未部署、push、merge。
- 测试仅有既存 Starlette/httpx deprecation warning；无失败、无 skip、无新增依赖。

状态：`machine-complete / v1.1-goal-review-pending / physical-validation-pending`。
