# AI-2026-005 Brief

Implement the production-facing, transport-neutral Qwen Realtime media service defined by `CONTRACT-AI-REALTIME-001 v1`.

Reuse the existing Qwen WebSocket behavior, but remove local `sounddevice` and file ownership from the production path. The service must accept Backend-authenticated Opus frames, decode/resample to Qwen PCM, return encoded Opus, and emit strict state events. It owns model transport and codecs only; it never executes NomaCook Tool or writes Backend state.
