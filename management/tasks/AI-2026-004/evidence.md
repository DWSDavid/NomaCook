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
- 收尾全量仓库回归：`python -m pytest -q` → `331 passed, 1 warning`，exit `0`；warning 仍为既有 Starlette/httpx deprecation。

### Scope and privacy

- 从任务开始 HEAD `178ef27333cf7cdb8d6156e22bc4000124dc8bd3` 到最终 HEAD 的变更路径全部位于
  `server/gateway/**`、`tests/model_service/**` 或本任务证据路径。
- `535f382...` 到任务开始 HEAD 的 6 个管理文件是任务激活时已有提交，不属于本执行者 Delta；未修改 `task.yaml` 或 `review.yaml`。
- `server/gateway` 禁止域扫描无匹配；凭据/私钥/长 Bearer 模式扫描无匹配。
- 未调用真实 Qwen；未连接 Node、Backend、App、Hardware、WebRTC；未 push、merge、deploy。

### Final machine boundary

服务实现包含严格 Request/Event DTO、512 KiB 请求/64 KiB 行/512 事件/8 KiB text delta/8 messages/2 Tools/60 秒约束、constant-time Bearer auth、bounded duplicate registry、Fake Qwen SSE transport、一次 Tool 校验、取消和 FastAPI readiness/stream。

结论：`machine-complete / manager-review-pending / integration-pending`。

## Closed P0 Delta — 2026-08-20

### Authority and root-cause evidence

- Reviewed Head `a2580386aee0d374b8338f804e2768d1e854ea89` 与 Manager Review
  `9c5a5be45bcc5f41f14da179f39ff16d1c646e04` 均为当前分支祖先；`task.yaml`、`review.yaml` 未修改。
- P0-001 最小复现读取公开 golden 请求并检查 `_payload`：修复前供应商名称为
  `nomacook.speak@1`；P0-002 Fake Transport 按 finish→usage→DONE 输入时，修复前事件为
  `response.accepted/message.start/text.delta/response.failed`，错误码
  `MODEL_RESPONSE_INVALID`。
- 根因：内部合同 Tool 名称直接写入供应商 `function.name`；Transport 在 finish choice 处立即
  发出 `stop`，Service 随后拒绝合法 usage 尾块。

### RED → GREEN

- RED：`pytest tests/model_service/test_qwen_transport.py tests/model_service/test_service.py -q`
  exit `1`，`9 failed / 21 passed`。
- GREEN：同一命令 exit `0`，`30 passed`。
- P0-001 GREEN 覆盖安全别名 payload、别名反向映射、未知别名、多 Tool/冲突 fail-closed。
- P0-002 GREEN 覆盖 text 与 Tool 的 finish→usage-only→DONE、usage 保留、multiple finish、
  finish 后 text/tool、缺 DONE fail-closed；Service 最终顺序为 usage 后唯一 message.end。

### Delta implementation

- 增加固定一对一映射：
  `nomacook.speak@1 ↔ nomacook_speak_v1`、
  `nomacook.submit_decision@1 ↔ nomacook_submit_decision_v1`；只向 Qwen 发送安全别名，返回后还原内部合同名。
- Transport 暂存唯一 finish_reason，拒绝 finish 后的非 usage-only chunk，要求 DONE，最后只 yield 一次 stop。
- P0 Delta 提交：`ec5cb67f36c3a7eb08848b613f9cb63934266aea`。

### Delta verification

- `tests/model_service`：`66 passed / 0 skipped`，exit `0`。
- `tests/test_vlm_contract.py tests/test_runtime_event_boundaries.py`：`21 passed`，exit `0`。
- `compileall -q server/gateway`：exit `0`；`git diff --check`：exit `0`。
- Delta changed paths from Manager Review：仅 `server/gateway/contracts.py`、
  `server/gateway/qwen_transport.py`、`tests/model_service/test_qwen_transport.py`、
  `tests/model_service/test_service.py`；均在 allowed paths。
- Forbidden-domain scan、secret-pattern scan：无匹配；未调用真实 Qwen、Node、Backend、App、Hardware、WebRTC。
- `git status --short`：仍仅 `?? .gitkeep`、`?? config.yaml`；两个文件未读取、未修改、未暂存、未提交。

Delta 状态：`manager-review-pending / integration-pending`。

## Manager Merge Closeout

- Manager Delta Review：`e5d3fb2c11b46bd6d3b6a248a76fa13520ce62ad`，`MVP-review-passed / backlog-recorded`。
- AI `agent/cv-live-camera` Merge Commit：`3d1fac90124200061291115bb5e9da861640c7a3`。
- 合并前与合并后均验证：model-service 66/66、0 skip；既有 VLM/Event 21/21；compileall、`git diff --check` 退出码 `0`。
- 合并后仍只有 `?? .gitkeep`、`?? config.yaml`；两者未读取、修改、暂存或提交。
- 真实 Node、Backend、Qwen、App、Hardware 和 WebRTC 未执行，继续由独立 Integration Task 负责。
