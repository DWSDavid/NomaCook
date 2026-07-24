# Handoff — Demo 终端日志 + SOP 菜名旁白(给 Codex)

> 2026-07-24。`server/pipeline/demo_log.py` 提供给评委看的干净 ✓ 播报。
> 成品菜不再调用模型识别；菜名直接取 SOP 的 `recipe.dish`，结果确定、无额外延迟。

## 任务 1:接 demo 日志

1. `run_pipeline.py` 加参数 `--demo-log`(BooleanOptionalAction,默认 True)。
2. 循环开始前建实例:`demo = DemoLogger(enabled=args.demo_log)`。
3. 在已有的调用点插入(都是现成变量,不要新算):
   - **进入新步骤时**(transition 后 / 首步初始化):
     `demo.step_enter(sequence=step.sequence, total=len(recipe.steps), title=step.title or step.instruction[:12])`
   - **每个 keyframe 出检测后**:`demo.detections([d.canonical_label for d in latest_canon])`
   - **握持/靠近事件 emit 时**:`demo.signal(f"手拿着{中文物名}")`(near/holding 各一句;
     中文名用 demo_log 里的 LABEL_ZH,缺的加进去)
   - **VLM 事件 emit 时**:`demo.vlm(phase, confidence, reason)`
   - **播 remark 时**:`demo.remark(speak)`
   - **每 keyframe 算完分**:`demo.score(score, step.completion_policy.threshold, hit=score>=threshold)`
   - **STEP DONE 时**:`demo.step_done(next_step.instruction if next_step else None)`
4. 不要删现有 stdout(工程师日志)。demo 日志是**叠加**,--demo-log 关掉就只剩旧日志。

## 任务 2:结尾 SOP 菜名旁白

1. 删除 `server/vlm/dish.py`，不增加模型，也不保留 `--identify-dish` 开关。
2. `complete_item()` 直接使用 SOP 菜名：
   `f"{recipe.dish}做好了。妈，我会做饭了。"`。
3. demo 日志在会话完成时调用 `demo.dish(recipe.dish)`；不显示识别置信度，
   也不暗示菜名来自视觉识别。

## 验收

```bash
.venv/bin/python harness/run_pipeline.py \
  --source <最新演示视频路径> --narrate gemini --run-tag demo_log_v1
```

1. 终端出现干净的 `▶ 第 x 步` / `✓ 看到:…` / `🔍 Gemini:…` / `进度 ▰▰… 达标!` / `✅ 完成 → …` 播报,读起来像一张跑动的清单。
2. 视频结尾旁白为 `番茄炒蛋做好了。妈，我会做饭了。`，终端菜名取自 SOP。
3. `--no-demo-log` 时回到旧的工程师日志,说明是纯叠加没破坏原有输出。
4. 全套测试仍绿(新模块是新文件,不该影响)。

## commit(按推送规则)

```
git add server/pipeline/demo_log.py server/pipeline/narrate.py harness/run_pipeline.py docs/
git commit -m "demo: clean tick-mark terminal log + scripted dish closer"
git push origin main
```

## 备注:检测 context

检测结果作为背景提示接入 Gemini 的工作由
`HANDOFF-vlm-detection-context.md` 负责；本地打分与 VLM 判断仍保持独立。

## 实测记录(执行后填写)

- demo 日志观感(贴 5-8 行样例):
- SOP 菜名旁白:
- --no-demo-log 回退是否正常:
- 遗留问题:
