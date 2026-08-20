# AI-2026-004 Evidence

## Manager Activation

- 人类负责人已批准设计、两份实施计划及 `CONTRACT-AI-MODEL-SERVICE-001 v1`。
- Management/Contract Revision：`e8803f0ed5e69ffc16580ccbd0438c58c6fe9400`。
- AI Base：`535f38286c05845b594c948f3ce7ed5a5d073827`。
- 激活阶段未修改业务代码、未读取私密 `config.yaml`、未调用模型。
- 开工前既有未跟踪状态仅为 `.gitkeep` 与 `config.yaml`，必须原样保留。

执行者必须追加实际 RED/GREEN、依赖安装、命令、退出码、测试数量及完整 Commit SHA。

## Executor Verification — 2026-08-20

### Task 0

- 当前任务分支：`codex/ai-model-service-v1`。
- 开工前分支/HEAD：`agent/cv-live-camera` /
  `178ef27333cf7cdb8d6156e22bc4000124dc8bd3`；任务基线
  `535f38286c05845b594c948f3ce7ed5a5d073827` 为祖先。
- 开工前唯一未跟踪状态为 `?? .gitkeep`、`?? config.yaml`；正文未读取，两个文件未被读取、复制、移动、暂存、提交、忽略或删除。
- `task.yaml` authority 检查通过：`status=ready`、`task_branch=codex/ai-model-service-v1`。

### Dependencies and tests

- `uv pip install -p .venv -r server/gateway/requirements.txt`：exit `0`；安装隔离服务依赖并未修改 requirements 或私有配置。
- Task 1 RED：exit `2`，`ModuleNotFoundError: server.gateway.contracts`；GREEN：`11 passed`。
- Task 2 RED：在 Task 1 commit 快照上运行认证/事件测试，exit `2`，`ModuleNotFoundError: server.gateway.errors`；GREEN：`9 passed`。
- Task 3 RED：exit `2`，`ModuleNotFoundError: server.gateway.qwen_transport`；GREEN：`13 passed`。
- Task 4 RED：exit `2`，`ModuleNotFoundError` for `tool_validation`/`service`；GREEN：`17 passed`。
- Task 5 RED：exit `2`，`ModuleNotFoundError: server.gateway.app`；GREEN：`9 passed`。
- Final service suite：`python -m pytest tests/model_service -q` → `59 passed, 0 skipped`，exit `0`。
- Existing unaffected focus：`python -m pytest tests/test_vlm_contract.py tests/test_runtime_event_boundaries.py -q` → `21 passed`，exit `0`。
- `python -m compileall -q server/gateway`：exit `0`；`git diff --check`：exit `0`。
- FastAPI TestClient 输出一项既有 Starlette/httpx deprecation warning；不影响通过结果。

### Scope and privacy

- 从任务开始 HEAD `178ef27333cf7cdb8d6156e22bc4000124dc8bd3` 到最终 HEAD 的变更路径全部位于
  `server/gateway/**`、`tests/model_service/**` 或本任务证据路径。
- `535f382...` 到任务开始 HEAD 的 6 个管理文件是任务激活时已有提交，不属于本执行者 Delta；未修改 `task.yaml` 或 `review.yaml`。
- `server/gateway` 禁止域扫描无匹配；凭据/私钥/长 Bearer 模式扫描无匹配。
- 未调用真实 Qwen；未连接 Node、Backend、App、Hardware、WebRTC；未 push、merge、deploy。

### Final machine boundary

服务实现包含严格 Request/Event DTO、512 KiB 请求/64 KiB 行/512 事件/8 KiB text delta/8 messages/2 Tools/60 秒约束、constant-time Bearer auth、bounded duplicate registry、Fake Qwen SSE transport、一次 Tool 校验、取消和 FastAPI readiness/stream。

结论：`machine-complete / manager-review-pending / integration-pending`。
