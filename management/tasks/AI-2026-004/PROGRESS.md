# AI-2026-004 Progress

- 目标：实现 `CONTRACT-AI-MODEL-SERVICE-001 v1` 的独立 Python AI Model Service。
- 当前状态：`ready / execution-pending`。
- AI 基线：`agent/cv-live-camera` / `535f38286c05845b594c948f3ce7ed5a5d073827`。
- 管理与合同版本：`e8803f0ed5e69ffc16580ccbd0438c58c6fe9400`。
- 任务分支：`codex/ai-model-service-v1`。
- 顺序：合同/golden → auth/registry/events → Qwen transport → Tool validation/service → FastAPI → 收口。
- 开工前既有状态：`?? .gitkeep`、`?? config.yaml`；必须原样保留且禁止读取正文。
- 本任务只使用 Fake Qwen Transport，不连接真实 Node、Backend 或模型。
-
## 执行记录

- Task 0：开工前分支 `agent/cv-live-camera`，HEAD 为
  `178ef27333cf7cdb8d6156e22bc4000124dc8bd3`；任务基线
  `535f38286c05845b594c948f3ce7ed5a5d073827` 为其祖先；状态严格为
  `?? .gitkeep`、`?? config.yaml`。任务 authority 检查通过，创建
  `codex/ai-model-service-v1`。
- Task 1 RED：`pytest tests/model_service/test_contracts.py -q`，exit `2`，缺少
  `server.gateway.contracts`；GREEN：`11 passed`，exit `0`。
- Task 2 RED：在 Task 1 完成快照 `bcb19f5c710c169ebade601192839cd9dab9ade3` 上运行认证/事件测试，exit `2`，缺少
  `server.gateway.errors`；GREEN：`9 passed`，exit `0`。
- Task 3 RED：`pytest tests/model_service/test_qwen_transport.py -q`，exit `2`，缺少
  `server.gateway.qwen_transport`；GREEN：`13 passed`，exit `0`。
- Task 4 RED：Tool/Service 测试 exit `2`，缺少 `tool_validation` 和 `service`；GREEN：`17 passed`，exit `0`。
- Task 5 RED：`pytest tests/model_service/test_app.py -q`，exit `2`，缺少 `server.gateway.app`；GREEN：`9 passed`，exit `0`。
- Task 6：`pytest tests/model_service -q` 为 `59 passed / 0 skipped`；既有不相关聚焦测试
  `tests/test_vlm_contract.py tests/test_runtime_event_boundaries.py` 为 `21 passed`；compileall 和
  `git diff --check` 均 exit `0`。

## 实现提交

- `bcb19f5c710c169ebade601192839cd9dab9ade3`：合同 DTO、依赖和 JSON/NDJSON goldens。
- `c987ef1631db4c152ca14a248269873b59fe311`：认证、错误、provider_call registry、事件边界。
- `99693b4adb548f50e35d5eeb859126e789bceb11`：单次 Qwen-compatible SSE Transport 和 Fake 测试。
- `dce2d7bd65f98f77dcdaf45783351e2a42de0347`：Tool Schema 校验和 provider-neutral Service。
- `dfff87da5f4762f76051be0d7e8f9c6f56303c0a`：FastAPI health/readiness/stream 服务。
- `e5bafe2876d225a0a1c5514ea7236d744343773f`、`69fc659`：脱敏测试夹具和扫描文本收口；最终完整 SHA 以 Git 核验为准。

当前执行状态：`machine-complete / manager-review-pending / integration-pending`。

- 收尾全量回归：`python -m pytest -q` → `331 passed, 1 warning`，exit `0`；无 skip。

## P0 Delta — 2026-08-20

- Delta 基线：Reviewed Head `a2580386aee0d374b8338f804e2768d1e854ea89` 与 Manager Review
  `9c5a5be45bcc5f41f14da179f39ff16d1c646e04` 均在当前分支祖先链；未修改 `task.yaml` 或 `review.yaml`。
- RED：P0 focused `tests/model_service/test_qwen_transport.py tests/model_service/test_service.py`
  exit `1`，`9 failed / 21 passed`；复现内部 Tool 名称直发供应商，以及 finish→usage→DONE 被
  service 收口为 `response.failed/MODEL_RESPONSE_INVALID`。
- GREEN：同一 P0 focused 命令 `30 passed`，exit `0`。
- GREEN：`tests/model_service` `66 passed / 0 skipped`，exit `0`；既有 VLM/Event focused `21 passed`。
- `compileall`、`git diff --check`、allowed-path 和 privacy scans 均通过；状态仍严格只有
  `?? .gitkeep`、`?? config.yaml`。
- P0 Delta 实现提交：`ec5cb67f36c3a7eb08848b613f9cb63934266aea`。

当前状态：`manager-review-pending / integration-pending`；等待针对两个 P0 的封闭式 Manager Delta Review。
