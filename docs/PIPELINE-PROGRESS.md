# MP4 端到端管线执行进度

> 计划:[PLAN-MP4-E2E-PIPELINE.md](./PLAN-MP4-E2E-PIPELINE.md)。
> 每完成一个 Task 追加一行;有偏离计划的决定(接口微调、阈值改动)必须在"备注"里写明。
> Manager(Claude)在每个 Wave 结束后 review 并在下方勾掉波次门。

| 日期 | Task | 执行者 | 结果 (tests/验收) | commit | 备注 |
|---|---|---|---|---|---|
| 2026-07-23 | Task 1 | Codex | 阻塞:`.venv/bin/python -m pytest tests/ -q` 实际 40 passed,计划要求 37 passed | — | 已按 Step 1-4 完成 TDD 实现,但全量验收数量与计划冲突;收集中已有 `tests/test_pipeline_session.py` 3 项测试。遵照冲突规则未 commit,未绕过验收。 |
| 2026-07-23 | Task 2 | OpenCode | 40 passed, clean_sessions --dry-run OK | c00703a | 无偏离 |
| 2026-07-23 | Task 1 复核 | Claude(manager) | 40 passed = 34 原有 + Task1 3 项 + Task2 3 项,diff 与计划逐字一致 | — | "37 passed" 是计划笔误(未计入并行 Task 2 的测试),已修正计划原文。裁定:Task 1 验收通过,Codex 可直接执行 Step 5 commit。 |
| 2026-07-23 | Task 1 收尾 | Claude(manager) | 40 passed;Task 1 三个文件按计划 message 提交 | 4b4cb79 | 代替 Codex 执行机械 Step 5;计划与进度文档另行提交(8b27077)。Wave 1 关闭。 |
| 2026-07-23 | Task 4 | OpenCode | 44 passed | bfdbc8d | 无偏离 |
| 2026-07-23 | Task 3 | Codex | 新增 5 项通过;全量 49 passed;engine smoke:`evidence_added evidence_added step_completed` | 2d8df4f | `load_script` 选择返回 `list[dict]`,每行注入 `_index`,并按 `pts_ms`、原始行号稳定排序。 |

## Wave 门(Claude 勾)

- [x] Wave 1 通过(Task 1 + 2,全量 pytest 绿,manager 复核 2026-07-23)
- [x] Wave 2 通过(Task 3 + 4,49 passed,接口抽查一致;load_script 形式已裁定并同步进计划 Task 5,manager 复核 2026-07-23)
- [ ] Wave 3 通过(Task 5 + 6,含手动确定性验收 `equal`)
- [ ] Wave 4 通过(Task 7,e2e 绿)
- [ ] Wave 5 通过(Task 8 + 9)
- [ ] 终验矩阵全部通过
