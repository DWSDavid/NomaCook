# AI-2026-004 Handoff

任务已由 Manager 激活，等待执行者从 `agent/cv-live-camera` 基线 `535f38286c05845b594c948f3ce7ed5a5d073827` 创建 `codex/ai-model-service-v1`。

唯一依据为批准的 `CONTRACT-AI-MODEL-SERVICE-001 v1`、设计、AI/Python 计划和本任务 `task.yaml`。只修改 allowed paths，使用 TDD 和原子提交。

根目录未跟踪 `.gitkeep` 和私密 `config.yaml` 属用户状态，禁止读取、复制、移动、暂存、提交、忽略或删除。本任务只能证明 Python 对 Fake Qwen Transport 的机器行为；不得声称 Node↔Python 或真实 Qwen Integration 完成。

## Executor Handoff — 2026-08-20

状态：`machine-complete / manager-review-pending / integration-pending`。

最终分支：`codex/ai-model-service-v1`。

实现提交（完整 SHA）：

- `bcb19f5c710c169ebade601192839cd9dab9ade3`
- `c987ef1631db4c152ca14a248269873b59fe311`
- `99693b4adb548f50e35d5eeb859126e789bceb11`
- `dce2d7bd65f98f77dcdaf45783351e2a42de0347`
- `dfff87da5f4762f76051be0d7e8f9c6f56303c0a`
- `e5bafe2876d225a0a1c5514ea7236d744343773f`
- `69fc659`（认证扫描文本收口；完整 SHA 见 Git）

交付边界：

- 独立 FastAPI Python AI Model Service，使用进程环境，不读取 `config.yaml` 或 dotenv；
- 合同 DTO、严格 NDJSON、Fake Qwen-compatible SSE、Tool Schema 校验和单一 Tool Call；
- constant-time service token、bounded provider_call_id registry、容量、取消、唯一终态和安全错误；
- `/health`、fail-closed `/ready`、`/v1/agent-model:stream`；
- `tests/model_service` `59 passed / 0 skipped`，既有 VLM/Event 边界测试 `21 passed`。
- 收尾全量仓库回归 `331 passed, 1 warning`，exit `0`；warning 为既有 Starlette/httpx deprecation。

后续 Integration：必须由独立任务启动真实 FastAPI 服务，注入 Fake Qwen Transport，连接真实 Node
production adapter，完成一次完整 Agent Turn；本任务不证明 Node↔Python、真实 Qwen、Backend、App、Hardware、WebRTC 或物理烹饪完成。
