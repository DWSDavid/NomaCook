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
| 2026-07-23 | Task 9 | OpenCode | not-e2e 53 passed 0 failed; e2e PASS (11s) | cad172c | 无偏离 |
| 2026-07-23 | Task 8 | Codex | not-e2e:`53 passed,1 deselected`;e2e:`1 passed`;off 模式未加载 `google.genai`/`server.vlm.client` | 14fe65a | StubClient 先红(`ModuleNotFoundError`)后绿。实机原命令:`$ .venv/bin/python harness/run_pipeline.py --source data/test_videos/synthetic_smoke.mp4 --device cpu --vlm gemini --max-frames 90 --keyframe-interval 1.0`，因 key 仅存在 `.env`、未导出而在初始化时报错:`RuntimeError: GEMINI_API_KEY is required for VLM calls`，未触发 VLM、未产出事件。随后仅在验收 shell 中执行 `set -a; source .env; set +a` 并重跑同命令，输出:`report -> .../run_20260723T165657/report.md`;`frames=45 events=0 transitions=0 final=in_progress`。核对:`vlm_mode=gemini`;关键帧为 `0/1000/2000ms`;score 低于 `question_min_score`，因此门控未触发真实 API，`events.jsonl` 未创建且无 `vlm.step_assessment`/`.stale`;未改 prompt/阈值，真实 API 响应验收待进入触发带的人工素材。实现按全局离线契约令 validation `received_at=requested_at=t_server_for(pts_ms)`，避免固定 epoch 请求被真实 `now()` 必然判 TTL stale。并发说明:Task 9 的 `cad172c` 在本 Task commit 前意外带入已完成的4处 runner 插入，本 commit 因此只含 `vlm_hook.py` 与 StubClient 测试；当前组合代码及回归均通过。 |
| 2026-07-23 | Task 10 | OpenCode | not-e2e 55 passed 0 failed | 2d35cbd | 无偏离 |
| 2026-07-23 | Task 11 | Codex | not-e2e:`58 passed,1 deselected`;e2e:`1 passed`;say 实机退出码 0、ffmpeg 缺失优雅降级 | 88f9af2 | 先红:`ModuleNotFoundError: server.pipeline.narrate`,后绿:`3 passed`。实机原命令:`$ .venv/bin/python harness/run_pipeline.py --source data/test_videos/synthetic_smoke.mp4 --device cpu --script tests/fixtures/tomato_egg_full_script.json --run-tag narrate_a --max-frames 120 --narrate say`<br>`[600/1200/1800/2400ms] STEP DONE x4`<br>`report -> data/sessions/ses_rv_tomato_egg_demo_1_synthetic_smoke/run_narrate_a/report.md`<br>`NARRATE ERROR (video kept, narration skipped): ffmpeg not found; install it first: brew install ffmpeg`<br>`frames=45 events=12 transitions=4 final=completed`;exit 0。本机 `Tingting zh_CN` 存在,未替换 voice。其余产物完好:`annotated.mp4` 924960 bytes、`events.jsonl`、`timeline.jsonl`、`meta.json`、`report.md`;`narration.json` 5 条(intro + 3 step + complete,本脚本无 question);因 ffmpeg/ffprobe 未安装,按计划不产出 `annotated_narrated.mp4`。off import 检查确认未加载 `google.genai`。 |
| 2026-07-23 | 飞书同步 | Codex | [NomaChef 完整同步 2026-07-23](https://scn9l51zoj9p.feishu.cn/wiki/AYQzwRDsSisxIwklSbCcl0BMnKg)<br>[番茄炒鸡蛋 · 每步系统上下文(工程契约)](https://scn9l51zoj9p.feishu.cn/wiki/EYOmwnAEiiTVfMklGSfcqHg2nEr)<br>[番茄炒鸡蛋 · UI 展示文案](https://scn9l51zoj9p.feishu.cn/wiki/KM63wgkqbiXA1DkMoDFcIFUqnue) | — | 3 篇 Markdown 已原样同步至指定父节点；15 张表格、2 个代码块及来源标注回读校验通过。 |

## Wave 门(Claude 勾)

- [x] Wave 1 通过(Task 1 + 2,全量 pytest 绿,manager 复核 2026-07-23)
- [x] Wave 2 通过(Task 3 + 4,49 passed,接口抽查一致;load_script 形式已裁定并同步进计划 Task 5,manager 复核 2026-07-23)
- [x] Wave 3 通过(Task 5 + 6,51 passed;manager 亲测确定性:mgr_1 vs mgr_2 `equal`,mgr_1 vs Codex det_1 跨会话 `equal`;CLI 契约与 render 接口抽查一致,2026-07-23)
- [x] Wave 4 通过(Task 7;manager 亲测 e2e PASS + 51 not-e2e 绿;diff 严格限于 6 个插入点,keyframe 写盘先于渲染,2026-07-23)
- [x] Wave 5 通过(Task 8 + 9;manager 裁定:①Task 9 commit 带入 runner 插入的纠缠不重写历史,以组合态验收;②Codex 的 received_at=t_server_for 偏离采纳,修复了固定 epoch 与真实 TTL 的矛盾,live 化时切回真实时钟,2026-07-23)
- [x] 终验矩阵 7/7 通过(manager,2026-07-23):54 passed 含 e2e;final_1/final_2 `equal`;script 模式 4 transitions + final=completed + report 齐全;标注 MP4 45/45 帧可读、keyframes 3 张原始帧;真实 Gemini VLM 1 次调用 accepted 入流(band script 顶分触发);clean_sessions --all 清空 10 个 run 目录。
  过程中发现并修复两个真 bug(manager 直改):①server/vlm/client.py 传 pydantic 模型作 response_schema 被 Gemini 400 拒(additionalProperties),改为显式 types.Schema;②runner 的 VLM 调用未隔离异常会炸掉整个 session 循环,已加 try/except 降级继续。
- [ ] Wave 6 通过(Task 10 骨架叠加 + Task 11 解说配音;2026-07-23 追加,前置:用户 brew install ffmpeg)
