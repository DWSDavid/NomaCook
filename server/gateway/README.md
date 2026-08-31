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

Required process environment for readiness:

```text
AI_MODEL_SERVICE_TOKEN
AI_MODEL_MAX_CONCURRENCY
AI_MODEL_REQUEST_TIMEOUT_MS
QWEN_AGENT_ENABLED=true
DASHSCOPE_API_KEY
BAILIAN_WORKSPACE_ID
QWEN_AGENT_MODEL=qwen3.6-flash
AI_MODEL_SERVICE_HOST=127.0.0.1
AI_MODEL_SERVICE_PORT=8090
```

Run locally with:

```bash
.venv/bin/python -m server.gateway.main
```
