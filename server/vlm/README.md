# VLM visual confirmation

This package is intentionally separate from Gemini Live. Live handles audio conversation; the VLM receives one selected frame and returns a small structured observation tied to a frozen `step_id` and `context_version`.

The default model is `gemini-3.6-flash`, configurable through `GEMINI_VLM_MODEL`. The adapter uses structured JSON output and does not set sampling parameters.

Every request carries `decision_id`, `step_id`, `context_version`, and `frame_id`. A response is marked stale and contributes no state-engine score when any identifier differs or when it arrives after the 8-second TTL. Stale results can still be emitted as audit events.

No model call can directly advance a step. Accepted observations become `vlm.step_assessment` evidence and must still satisfy the state engine's weighted and consecutive-hit policy.
