# Handoff — "听得见 Gemini" 最终回归(给 Codex / OpenCode)

> 2026-07-24。背景:此前 Gemini VLM 虽已自动启用(9 秒一查),但它的判断
> **只进打分引擎和 stdout 日志,从不进旁白、不上画面**,所以成片里感觉不到
> AI 的介入。本轮补上了 CLAUDE.md §2 巡检第 4 问("有什么值得主动说的?"):
> Gemini 现在能开口了。单测 66/66 已过,待真实录像回归。

## 本轮代码变化(已完成,勿重复实现)

| 文件 | 变化 |
|---|---|
| `server/vlm/schema.py` | VLMObservation 新增可选 `coach_comment`(一句主动提醒或 null) |
| `server/vlm/client.py` | 响应 schema + system prompt 增加第 6 条:值得说才说,大部分应为 null |
| `server/pipeline/narrate.py` | 新增 `remark_item`(kind="remark") |
| `harness/run_pipeline.py` | VLM 事件→旁白:risk warning/critical 优先播("注意,…"),否则播 coach_comment;去重 + 20 秒最小间隔 + 每步最多 2 条;GEMINI 观察同时上 annotated 视频右侧事件栏 |
| `server/pipeline/report.py` | report.md 新增 "Gemini observations" 表(每次调用的 phase/conf/risk/reason/coach_comment;若为空会明确提示) |

## 执行任务

### 1. 用上次同一段真实录像回归

```bash
cd ~/Documents/NomaChef
.venv/bin/python harness/run_pipeline.py \
  --source data/test_videos/test2.mov \
  --narrate gemini --run-tag gemini_audible_v1
```

(VLM 已随 GEMINI_API_KEY 自动启用,无需 --vlm 参数;如当前分支参数不同以 --help 为准。)

### 2. 验收清单(全部要留证据)

1. stdout 有 `GEMINI phase=... confidence=... reason=...` 行,**数一下总次数**
   (预期约每 9 秒一次,3 分钟视频 ≈ 15-20 次)。
2. stdout 出现至少 1 条 `REMARK ...` 行(Gemini 主动开口)。若 0 条:打开
   report.md 的 Gemini observations 表,确认 coach_comment 列是否全为 `-`;
   全为空说明模型太沉默,把 `client.py` prompt 第 6 条里的"大部分图片应该是
   null"放宽为"每个步骤至少值得说一句",重跑一次对比。
3. `report.md` 的 Gemini observations 表**非空**,逐行读 reason,标记明显误判的
   行(这是明天调 prompt 的弹药)。
4. `annotated_narrated.mp4`:能听到 remark 的语音(Aoede 音色),画面右侧事件栏
   能看到 GEMINI 行;预告(preview)出现在 STEP DONE 之前且不显突兀。
5. transitions ≥ 2(不低于 v4 基线);锅在 timeline 中仍为 wok 非 bowl。

### 3. 产出"更细节的响应和反馈"(写进 run 目录的 report.md 末尾)

追加一节 `## Detailed feedback vs v4`,逐步骤写:
- 每个 step:进入/离开时间、preview 播报时间、Gemini 对该步的判断序列
  (phase 变化轨迹)、remark 内容、与 v4 同段落的差异。
- 专列一小节 "Gemini 误判清单":reason 与画面不符的调用(带 pts + 关键帧文件名)。
- 一句话总评:成片里"AI 介入感"够不够,不够的话瓶颈在哪(remark 频率?
  reason 质量?语音时机?)。

### 4. 通过后 commit + push(按推送规则)

```bash
git add server/ harness/ tests/ docs/
git commit -m "pipeline: make Gemini audible (coach_comment remarks, report observations, overlay)"
git push origin main
```

### 5. 明天的完整 AI 视频到手后

同一条命令换 `--source` 路径即可,其他全部不动;先跑一遍出 report,再决定调参。

## 6. 可选实验:FastSAM 切工探针(IngredSAM-lite,半天 timebox)

不 vendor FoodSAM/IngredSAM(mmcv 依赖地狱 + SAM 级延迟),用 ultralytics
内置 FastSAM 复刻其思路:分割 + HSV 认色,量化"番茄切了几块、块多大"。
离线跑,只吃已有 keyframes,不碰实时循环:

```bash
.venv/bin/python harness/probe_seg.py \
  --run-dir data/sessions/ses_rv_tomato_egg_demo_1_test2/run_real_test2_v4
```

首次运行自动下载 FastSAM-s.pt(~23MB)到 weights/。产出:每帧叠加图 +
`probe_seg/metrics.jsonl`(红/黄块数、面积中位数、延迟)。

**判定标准(写进实测记录):**
- 备菜段 keyframe 上 tomato_red count 随切菜进行**单调上升** → 信号可用,
  下一轮把它接成 step_01 的 evidence_rule(权重 0.2)。
- count 乱跳或把锅/手认成番茄 → 结论"分割信号在胸前视角不可用",
  记录证据后放弃,不再讨论 SAM 系接入。

## 实测记录(执行后填写)

- GEMINI 调用次数 / REMARK 条数: `run_gemini_audible_v2` 共 16 次 assessment、5 条 REMARK；首次 v1 的 15 次调用全部 `coach_comment=null`，按验收分支放宽 prompt 后重跑得到 REMARK。
- Gemini 误判行数 / 典型例子: 严格 reason-vs-frame 为 1 条（39108ms 把碗中已有四个蛋黄说成“正在打蛋”）；若计入 phase 回退则共 3 条，另两条在 57158ms、84233ms 将热锅/热油过程判回 `not_started`。详见 v2 `report.md`。
- 听感与介入感评价: `annotated_narrated.mp4` 已生成，AAC 音轨 122.296s；48–56s 音频 mean -20.3dB/max -5.3dB，画面可见 GEMINI overlay。5 条 remark 让介入感可感知，但后半段建议略重复。Gemini TTS 3.1 遇到 503/断连，补了有限重试和 clip 复用，缺失片段以 2.5 flash TTS + Aoede 完成。
- transitions 数 vs v4: 2 vs 2，未低于基线；两版最终均停在 `step_03_soften_tomato / in_progress`。
- 遗留问题: step_01 在 18050ms 预告时案板仍是完整去皮番茄，27075ms 仍在去蒂却已 STEP DONE；锅识别总体改善但 66183–78216ms 连续 5 个 keyframe 仍只报 bowl。下一任务用 FastSAM 物理切散信号门控 step_01 preview。
