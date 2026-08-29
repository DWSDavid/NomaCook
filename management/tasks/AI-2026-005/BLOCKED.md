# AI-2026-005 Blocked

Current blockers:

- `P0-AI-2026-005-001`: production entrypoint does not mount the Realtime WebSocket.
- `P0-AI-2026-005-002`: valid incremental audio can fail codec framing and `audio_done` can precede binary audio.
- `P0-AI-2026-005-003`: announce reports completed before any correlated exact audio result.

真实 Qwen、Backend、Node、App、Hardware 和物理设备属于后续 Integration，不是本任务机器层完成条件。

All reproduce within the allowed machine scope and must be fixed in one closed
Delta. Real Qwen and cross-end physical work remain integration pending.
