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

## P0 Delta Handoff — 2026-08-20

- 已封闭修复两个 Review P0，提交：`ec5cb67f36c3a7eb08848b613f9cb63934266aea`。
- P0-001：内部 Tool 合同名保持不变；Qwen payload 使用 `nomacook_speak_v1` /
  `nomacook_submit_decision_v1`，返回别名严格反向映射，未知/多 Tool fail-closed。
- P0-002：finish_reason 暂存；仅接受最终 choices=[] usage 尾块和 `[DONE]`，然后输出唯一 stop；
  finish 后 text/tool、multiple finish、缺 DONE 均 fail-closed。
- P0 focused `30 passed`；service 全量 `66 passed / 0 skipped`；既有边界 `21 passed`。
- 已通过 Manager Delta Review，并以 Merge Commit `3d1fac90124200061291115bb5e9da861640c7a3` 进入 `agent/cv-live-camera`。合并后 model-service、既有边界和 compileall 全部通过。
- 最终状态：`done / MVP-review-passed`。不处理 P1/P2；真实 Node↔Python、Qwen、Backend、App、Hardware 和 WebRTC 保持后续 Integration。
