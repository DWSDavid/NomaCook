# AI-2026-005 Progress

## 2026-08-31 physical P0-005 activation

Status: `ready / blocked-by-P0-005 / physical-validation-pending`.

- Opening reviewed HEAD: `dd7ad983ef6502a5d86980f99478742ffc81b7ae`.
- Physical sequence: Agent announce entered speaking, user speech moved to thinking, announce failed, but no normal response followed.
- Fix only interrupted-announce to fresh-user-response correlation in the existing Realtime service; no model call or deployment.

## 2026-08-30 v1.1 goal-level Review

Status: `machine-complete / MVP-review-passed / physical-validation-pending`.

Manager reviewed the complete AI v1.1 Delta together with all four Domain candidates and the passing exact-archive Gate. No current-path P0 was found; no isolated rerun was performed.

## 2026-08-30 Realtime v1.1 activation

Status: `ready / v1.1-machine-execution-pending / physical-validation-pending`.

- Base: `6f9047ad779773e4ea6500b2ecc67c0805dc3ca0` on `agent/cv-live-camera`.
- Task branch: `codex/ai-realtime-model-service-v1-1`.
- Scope is one complete schema/correlation/frame-count/partial-PCM/announce Delta; v1.0 paths remain frozen.
- Provider calls, config.yaml reads, deployment and isolated Manager Review are forbidden.

Status: `ready / execution-pending`.

- Base: `d8e68742d1cc3730dc19f097ad90a2d585b2e40a`.
- Contract/Management: `6d5d088d57e558277c858926b8497140151d9f85`.
- Branch: `codex/ai-realtime-model-service-v1`.
- Existing untracked `.gitkeep` and private `config.yaml` must remain untouched and unread.
- No real Provider call is authorized.

## Closed P0 Delta — 2026-08-29

- Delta 基线：Manager Review `5def6fdf39e234b579852061d076944b96b8dd57` 与 reviewed implementation
  `1d43f615470a7431e95afad2c9ac807fcecd1fe0` 均在当前分支祖先链；`task.yaml/review.yaml` 未修改。
- RED：生产入口测试 `2 failed / 2 passed`（`create_production_app` 不存在）；Realtime session P0
  focused `3 failed / 8 passed`（announce 提前完成、未支持 response_done/任意 PCM）；均为 exit `1`。
- GREEN：`tests/realtime` `29 passed / 0 skipped`，exit `0`；既有 `tests/model_service` `66 passed / 0 skipped`，exit `0`。
- `compileall -q server/realtime server/gateway`、`git diff --check`、allowed-path/privacy scans 均通过。
- 实现/测试 Commit：`7c6e9ae29d80b7fb01c311d530cbac4c48863a1a`。

当前状态：`blocked-by-P0 / manager-final-delta-review-pending / integration-pending`。

Manager closed Delta Review confirms P0-001/002/003 are fixed. One P0 newly
introduced by production composition remains: combined `/ready` reports 200
when Realtime is ready but the retained Agent Model Service is absent. Fix only
that readiness invariant.

## Final Readiness-only Delta — 2026-08-29

- Opening HEAD：`2ca5605949b2ca07d97954fb78b53858c631c6d5`；implementation/test
  commit：`ae872c00c51a5f2252a616ab95aeee2340a9e3ce`；opening HEAD remains an
  ancestor；`task.yaml/review.yaml` 未修改。
- RED：`./.venv/bin/python -m pytest tests/realtime/test_app.py -q`，exit `1`，
  `2 failed / 5 passed`；失败原因是组合生产入口尚未接收 Agent readiness 参数。
- GREEN：同一命令 `7 passed / 0 skipped`，exit `0`。新增断言覆盖 Realtime ready +
  Agent unready → `503`、Agent ready + Realtime unready → `503`、双方 ready →
  `200`，并确认 Provider factory calls `0`；同时保留 Agent Model 与 Realtime
  两条生产路由。
- 回归：`tests/realtime` `31 passed / 0 skipped`，`tests/model_service`
  `66 passed / 0 skipped`，均 exit `0`；`compileall -q server/realtime
  server/gateway` 与 `git diff --check` 均 exit `0`。
- 生产路径未打开本机音频设备或写会话文件；未读取、修改、移动、暂存、提交、忽略或删除
  `config.yaml`、`.gitkeep`；未调用真实 Provider、未启动其他端、未部署、未 push、未 merge。

当前状态：`machine-complete / manager-final-delta-review-pending / integration-pending`。

## Realtime v1.1 Executor Delta — 2026-08-30

- 接管 HEAD：`a3e662245497821671823359aadb9fdcaddecf6d`；基线
  `6f9047ad779773e4ea6500b2ecc67c0805dc3ca0`、management revision
  `7dba37cd0e732011b86bfd797f4663c37bb2a185`、contract/design/plan revision
  `1249c2e0b6b0d7e367aaec89402c9043b904a044` 均已核验；task/review 未修改。
- RED：`./.venv/bin/python -m pytest tests/realtime -q`，exit `1`，`15 failed / 21 passed`；
  v1.1 测试夹具已切换但实现仍声明 schema `1.0`，且缺少响应关联、尾部补齐和 announce 完整顺序。
- GREEN：同一命令 `39 passed / 0 skipped`，exit `0`；覆盖 schema `1.1` 严格拒绝旧版本/未知字段、
  text-first/audio-first、统一 `utterance_id`、announce `message_ref`、`output_frame_count`、
  partial PCM 静音补齐、zero-audio fail-closed、announce 三态、全 Session packet/timestamp 及
  control sequence 连续性。
- 回归：`tests/model_service` `66 passed / 0 skipped`，exit `0`；compileall 与 diff-check 通过。
- 实现/测试 Commit：`502f5987467654108568a786c9af2209d9d19913`。
- Fake Provider only，Provider calls `0`；未读取或处理 `config.yaml`、`.gitkeep`，未启动其他端、部署、
  push 或 merge。

当前状态：`machine-complete / v1.1-goal-review-pending / physical-validation-pending`。

## P0-005 Closed Delta — 2026-08-31

- Opening HEAD：`a2ab4947eb1d242cff6716b8543165b206046072`；实现/测试 Commit：
  `5a916ebfbfe6dcb6320e80a918a24912017df873`；opening HEAD 为其祖先；task/review 未修改。
- RED：`./.venv/bin/python -m pytest tests/realtime/test_session.py::test_interrupted_announce_quarantines_late_terminal_before_new_user_response -q`，
  exit `1`；迟到旧 announce 音频/cancelled terminal 产生额外失败事件并可污染新 owner。
- GREEN：同一聚焦场景 `1 passed / 0 skipped`，随后 `tests/realtime` `40 passed / 0 skipped`、
  `tests/model_service` `66 passed / 0 skipped`，均 exit `0`；announce.failed 恰好一次，迟到旧事件静默丢弃，
  新用户 response 使用新 utterance，thinking/text/started/done 各一次，binary 与 outbound 顺序正确且无 session.failed。
- compileall、git diff-check、允许路径与脱敏扫描通过；Fake Provider calls `0`；未读取或处理 `config.yaml`、
  `.gitkeep`，未调用真实 Qwen，未启动其他端、部署、push 或 merge。

当前状态：`machine-complete / P0-005-review-pending / physical-validation-pending`。

## Executor Verification — 2026-08-29

- 接管盘点：AI 仓库 `codex/ai-realtime-model-service-v1`，HEAD
  `d44c6a3ac7668d50b8b40bbd6898d0622a30c44e`；基线
  `d8e68742d1cc3730dc19f097ad90a2d585b2e40a` 为祖先；Management/Contract Commit
  `6d5d088d57e558277c858926b8497140151d9f85` 已存在。
- 开工前仅有 `?? .gitkeep`、`?? config.yaml`；两个文件未读取、未修改、未移动、未暂存、未提交、未忽略、未删除。
- RED：`pytest tests/realtime -q` exit `2`，缺少 `server.realtime`/`server.gateway.realtime_app`。
- GREEN：`tests/realtime` `19 passed / 0 skipped`，exit `0`；`tests/model_service` `66 passed / 0 skipped`，exit `0`。
- `compileall -q server/realtime server/gateway` 与 `git diff --check` 均 exit `0`。
- PyAV 实际 Opus roundtrip 通过 20ms 音频边界、48kHz/2ch↔16kHz/mono 和 24kHz/mono→48kHz/2ch；Fake Provider 覆盖 VAD、assistant text/audio、announce、pause/stop、interrupt、generation、backpressure。
- 实现/测试 Commit：`e12188b3817373e3effdc71703364d20b9f15372`。

当前状态：`blocked-by-P0 / manager-delta-review-pending / integration-pending`。

Manager initial Review found three complete-path P0s: the production Uvicorn
entrypoint does not expose the Realtime app; arbitrary Provider PCM deltas and
the split event/audio drains break the required wire order; and announce is
reported completed before correlated audio/text exists. One closed Delta must
fix all three in this task. No other scope is reopened.
