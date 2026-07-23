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
| 2026-07-23 | Task 6 | OpenCode | 51 passed | c6c53e7 | 无偏离 |
| 2026-07-23 | Task 5 | Codex | 51 passed;验收 A 4 次 STEP DONE + `final=completed`;验收 B `equal` | d718650 | A:`$ .venv/bin/python harness/run_pipeline.py --source data/test_videos/synthetic_smoke.mp4 --device cpu --script tests/fixtures/tomato_egg_full_script.json --run-tag manual_a --max-frames 120`<br>`[600ms] STEP DONE step_01_prepare -> step_02_scramble_egg`<br>`[1200ms] STEP DONE step_02_scramble_egg -> step_03_soften_tomato`<br>`[1800ms] STEP DONE step_03_soften_tomato -> step_04_combine_and_plate`<br>`[2400ms] STEP DONE step_04_combine_and_plate -> SESSION COMPLETE`<br>`frames=45 events=12 transitions=4 final=completed`<br>产物:`events.jsonl`,`timeline.jsonl`,`keyframes/kf_000000_0ms.jpg`,`meta.json`。<br>B1:`$ .venv/bin/python harness/run_pipeline.py --source data/test_videos/synthetic_smoke.mp4 --device cpu --script tests/fixtures/tomato_egg_full_script.json --run-tag det_1 --max-frames 120`<br>`[600/1200/1800/2400ms] STEP DONE x4; frames=45 events=12 transitions=4 final=completed`<br>B2:`$ .venv/bin/python harness/run_pipeline.py --source data/test_videos/synthetic_smoke.mp4 --device cpu --script tests/fixtures/tomato_egg_full_script.json --run-tag det_2 --max-frames 120`<br>`[600/1200/1800/2400ms] STEP DONE x4; frames=45 events=12 transitions=4 final=completed`<br>Compare:`$ .venv/bin/python -m server.events.replay compare data/sessions/ses_rv_tomato_egg_demo_1_synthetic_smoke/run_det_1/events.jsonl data/sessions/ses_rv_tomato_egg_demo_1_synthetic_smoke/run_det_2/events.jsonl`<br>`equal`。关键帧直接由未标注原始 `frame` 写入。 |
| 2026-07-23 | Task 7 | OpenCode | e2e PASS (49s), not-e2e 51 passed 0 failed | d022036 | 无偏离 |

## Wave 门(Claude 勾)

- [x] Wave 1 通过(Task 1 + 2,全量 pytest 绿,manager 复核 2026-07-23)
- [x] Wave 2 通过(Task 3 + 4,49 passed,接口抽查一致;load_script 形式已裁定并同步进计划 Task 5,manager 复核 2026-07-23)
- [x] Wave 3 通过(Task 5 + 6,51 passed;manager 亲测确定性:mgr_1 vs mgr_2 `equal`,mgr_1 vs Codex det_1 跨会话 `equal`;CLI 契约与 render 接口抽查一致,2026-07-23)
- [x] Wave 4 通过(Task 7;manager 亲测 e2e PASS + 51 not-e2e 绿;diff 严格限于 6 个插入点,keyframe 写盘先于渲染,2026-07-23)
- [ ] Wave 5 通过(Task 8 + 9)
- [ ] 终验矩阵全部通过
