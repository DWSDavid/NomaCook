# AI-2026-005 Blocked

Current blockers:

- `P0-AI-2026-005-001`: production entrypoint does not mount the Realtime WebSocket.
- `P0-AI-2026-005-002`: valid incremental audio can fail codec framing and `audio_done` can precede binary audio.
- `P0-AI-2026-005-003`: announce reports completed before any correlated exact audio result.

真实 Qwen、Backend、Node、App、Hardware 和物理设备属于后续 Integration，不是本任务机器层完成条件。

原始 Review 的三个 P0 已在同一任务分支完成封闭式 Delta；当前无新增机器层阻塞，等待 Manager
复审。真实 Qwen、Backend/Node/App/Hardware、Pi WebRTC 和物理验收继续保持
`integration-pending / physical-validation-pending`，不在本任务执行。

Current sole blocker: `P0-AI-2026-005-004`. Combined production readiness must
be 503 unless both Agent Model Service and Realtime are ready. The original
three P0s remain closed.

All reproduce within the allowed machine scope and must be fixed in one closed
Delta. Real Qwen and cross-end physical work remain integration pending.
