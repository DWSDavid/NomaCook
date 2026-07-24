# Handoff — 预告时机 + 锅碗消歧 + 自然语音 回归实测

> 给 Codex 的执行清单。2026-07-24 改动,单测 63/63 已过,
> 需要在真实录像上回归 + 听感验收。改动背景见文末。

## 1. 回归命令(用 v4 同一段真实录像)

```bash
cd ~/Documents/NomaChef
.venv/bin/python harness/run_pipeline.py \
  --source <v4 用的那段真实录像路径> \
  --vlm gemini --narrate gemini --run-tag regress_timing_v1
```

注意:`--vlm gemini` 必须显式带上(默认 off,不带就没有 Gemini 介入);
`--narrate gemini` 走新的 TTS 音色配置(音色可用 `GEMINI_TTS_VOICE` 环境变量换,
默认 Leda;候选:Kore、Aoede、Zephyr)。

## 2. 验收点

1. **预告时机**:stdout 出现 `PREVIEW step_xx score=...` 行,且发生在对应
   `STEP DONE` 之前;narration.json 里每个非末尾步骤有一条 `"kind": "preview"`,
   文案是"这一步差不多快好了。等下咱们要……"。
2. **锅不再是 Bowl**:timeline.jsonl 的 detections 里,灶上锅的位置应为
   `wok`;若仍出现大框 bowl,记录该关键帧文件名。
3. **听感**:`annotated_narrated.mp4` 里语气应明显比 v4 的 Tingting 自然;
   若仍播音腔,换 `GEMINI_TTS_VOICE=Kore` 再合成一次对比。
4. 全部 4 步 transition 仍然发生(不少于 v4 的 2 次;权重本轮未动,
   如 transition 变少即是回归,回报)。

## 3. 通过后 commit(按推送规则)

```bash
git add server/perception/context.py server/pipeline/narrate.py \
  harness/run_pipeline.py tests/
git commit -m "pipeline: pre-announce timing band, wok/bowl suppression, natural TTS voice"
git push origin main
```

## 4. 改动清单(供排查)

| 文件 | 改了什么 |
|---|---|
| `server/perception/context.py` | wok 提示词加 "black wok";bowl 提示词加 "small mixing bowl";BOTTLE_CONFUSERS 增 bowl→wok;新增 `_suppress_confusions`(IoU≥0.5 且 wok conf ≥ bowl conf−0.15 时压制 bowl) |
| `server/pipeline/narrate.py` | 新增 `preview_item`(快完成预告);intro/transition 文案口语化;gemini TTS 加音色 + 风格前缀 |
| `harness/run_pipeline.py` | emit() 里加预告触发:score ≥ threshold×0.75 时播一次预告(每步一次) |
| `tests/` | 新增 4 个测试(消歧×2、预告×2) |

## 5. 调参入口(如果实测不理想)

- 预告太早/太晚:`run_pipeline.py` 里 `preview_band = 0.75`
- 真碗被误杀:`context.py` 里 `_CONFUSION_CONF_MARGIN = 0.15`(调小更保守)
- 压制不生效:`_CONFUSION_IOU = 0.50`(调小更激进)

## 实测记录(执行后填写)

- 日期/录像:
- PREVIEW 行数 / 位置合理性:
- wok/bowl 修复情况:
- 语音听感(voice 名):
- transitions 数:
