# Python AI Model Service v1

This package implements `CONTRACT-AI-MODEL-SERVICE-001 v1` for the bounded
`agent_tool_stream_v1` capability. It owns provider transport and NDJSON event
adaptation only; it does not own Cooking state, Tools, prompts, or business
side effects.

The service reads configuration only from its process environment. Local
development uses the existing repository `.venv` and the isolated dependency
list in `server/gateway/requirements.txt`.

The HTTP surface is implemented in `server.gateway.app` and exposes `/health`,
`/ready`, and `/v1/agent-model:stream`.
