# AI-2026-005 Handoff

Execute `task.yaml` against the approved Realtime contract. Start with strict contract and codec RED tests, then implement one transport-neutral service batch.

Do not read `config.yaml`, call Qwen, open local microphone/speaker, start other services, or add business Tool logic. Stop after focused GREEN and two commits for Manager Review.

## Executor Handoff — 2026-08-29

状态：`machine-complete / manager-review-pending / integration-pending`。

实现/测试 Commit：`e12188b3817373e3effdc71703364d20b9f15372`。

本轮交付：

- 独立 `server/realtime` transport-neutral provider/session、严格控制信封和 Opus binary framing；
- PyAV 实际 Opus codec roundtrip 与 20ms 边界重采样；
- Fake Provider 驱动的 semantic VAD、assistant text/audio、interrupt、announce、pause/resume/stop、generation 和 backpressure；
- authenticated internal WebSocket `/v1/realtime-sessions:stream`、`/health`、fail-closed `/ready`；
- `tests/realtime` `19 passed / 0 skipped`，既有 `tests/model_service` `66 passed / 0 skipped`。

边界：生产路径不打开 `sounddevice`、本机麦克风或扬声器，不写 session/媒体文件，不执行 NomaCook Tool，不写 Backend 状态；真实 Qwen/Backend/Node/App/Hardware/WebRTC 和物理验收保持 Integration pending。

根目录既有 `?? .gitkeep`、`?? config.yaml` 保持原样且未读取。等待 Manager 对本轮唯一实现 Delta 进行 Review；不得处理后续 P1/P2 或启动真实 Provider。

## Manager P0 Delta

Fix all findings in one closed Delta:

1. Register Realtime routes/settings in the production service and gate both
   readiness and WebSocket admission before Provider construction.
2. Wait for session.updated, normalize arbitrary PCM fragments, preserve one
   ordered text/binary output stream, and fail closed on non-completed response.
3. Correlate announce through terminal audio/transcript; only exact authorized
   text may release buffered audio and produce announce.completed.

Add focused RED/GREEN for every listed reproduction, retain the existing 85
passing tests, submit one implementation/test Commit and one evidence Commit,
then stop for closed Delta Review. Real Provider calls remain 0.
