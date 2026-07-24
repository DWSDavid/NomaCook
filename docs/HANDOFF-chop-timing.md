# Handoff — 切工进度信号驱动预告时机(Codex target)

> 2026-07-24。产品决策:预告("这一步快好了,下一步是……")的时机必须由
> **物理状态**决定:案板上番茄已切散成小块 → 预告;还有完整大番茄 → 闭嘴。
> 现在的预告只看抽象分数(阈值 85%),感知不到切没切散。本任务把
> FastSAM 切工信号接成七步 SOP 中 step_03 的证据,让分数(从而预告)跟着案板走。
> 前置:docs/HANDOFF-gemini-audible-final.md 的主回归若未跑,先跑它。

## 第 0 步(硬门槛):探针验证,不过就停

```bash
cd ~/Documents/NomaChef
.venv/bin/python harness/probe_seg.py \
  --run-dir data/sessions/ses_rv_tomato_egg_demo_1_test2/run_real_test2_v4
```

看 `probe_seg/metrics.jsonl` + 叠加图,在**切菜时间段**内验证两条:
1. `tomato_red` 的 count 大体单调上升(切得越多块越多)
2. 最大红块的 area_frac 大体单调下降(整番茄消失)

**任一不成立 → 停止,把证据(帧名 + 数值)写进本文档实测记录,不做后续接线。**
成立 → 把你观察到的真实数值(切散时的典型 count、最大块 frac)记下来,
用于第 1 步的阈值,不要用拍脑袋数。

## 第 1 步:实现 chop_progress 信号

1. 新建 `server/perception/chop_progress.py`:
   - 复用 `harness/probe_seg.py` 的 `piece_metrics` / `classify_mask_color`
     (把这两个纯函数移到本模块,probe_seg 改为从这里 import,保持单测有效)。
   - 纯函数 `chop_state(rows) -> tuple[state, confidence]`:
     `not_chopped` / `chopping` / `mostly_chopped`。
     `mostly_chopped` 定义(阈值用第 0 步实测值,以下是占位):
     红块 count ≥ 6 且最大红块 area_frac ≤ 0.008。
     confidence 随 count 超出阈值的裕度上升,封顶 0.9。
2. `harness/run_pipeline.py` 加 `--seg`(choices: auto/off,默认 auto):
   - auto = FastSAM 权重存在或可下载时启用;初始化失败降级 off 并打日志,
     绝不让分割问题炸主流程。
   - **只在 keyframe 采样时刻跑**(跟现有 3 秒 sampler 同节奏),且只在
     当前步骤的 SOP 里存在 chop 证据规则时跑(七步 SOP 中为 step_03)。
   - 产出事件 `perception.chop_progress`,payload:
     `{"step_id": ..., "state": ..., "pieces": count, "largest_frac": ...}`,
     source `fastsam_chop_v1`。
   - stdout 打 `CHOP state=... pieces=... largest=...`;同时把
     `CHOP mostly_chopped x12` 追加进 recent_event_texts(上画面)。
   - 单次分割 > 500ms 时打 warning(MPS 正常约 50-100ms)。
3. `sop/tomato_egg.json` step_03 增加证据规则:
   ```json
   {
     "id": "prep_chopped",
     "event_type": "perception.chop_progress",
     "payload_matches": {"step_id": "step_03_cut_tomatoes", "state": "mostly_chopped"},
     "weight": 0.25,
     "min_confidence": 0.5
   }
   ```
   其他权重不动。效果:没切散时分数到不了预告带,预告自然被物理状态门控。
4. 测试:chop_state 阈值纯函数测试(≥4 条,含边界);现有全套测试保持绿。

## 第 2 步:回归验收(test2.mov)

```bash
.venv/bin/python harness/run_pipeline.py \
  --source data/test_videos/test2.mov \
  --narrate gemini --run-tag chop_timing_v1
```

验收点(全部留证据在实测记录):
1. stdout 中 step_03 段出现 `CHOP` 行,state 轨迹符合画面(先 not_chopped/
   chopping,切散后 mostly_chopped)。
2. **step_03 的 `PREVIEW` 行出现在首个 `CHOP mostly_chopped` 之后**、
   `STEP DONE step_03` 之前。这是本任务的核心验收。
3. 对照 v4:step_03 预告时刻对应的关键帧里,案板上应是碎块而非整番茄
   (贴出该关键帧文件名)。
4. transitions 不少于 v4;端到端 wall_seconds 不显著恶化(keyframe 才跑
   FastSAM,预期影响 < 5%)。
5. report.md 照旧追加 Detailed feedback(含 chop 信号轨迹一节)。

## 第 3 步:commit + push(按仓库推送规则)

```
git add server/ harness/ sop/ tests/ docs/
git commit -m "pipeline: chop-progress signal gates step_01 preview timing (FastSAM keyframe evidence)"
git push origin main
```

## 后续(本轮不做,只留一行)

同一信号换个颜色即可泛化:egg_yellow 散布在锅内 = 炒蛋成块(step_02)、
red_yellow 混合度 = step_04。等 step_01 跑通再谈。

## 实测记录(执行后填写)

- 探针单调性验证(帧号 + 数值): **失败，停止接线。** 切菜/备菜段 `tomato_red count` 并未上升：`kf_000000_0ms.jpg` 8（整颗番茄）→ `kf_000180_3008ms.jpg` 9 → `kf_000540_9024ms.jpg` 7 → `kf_001080_18049ms.jpg` 2（仍是 4 个完整去皮番茄）→ `kf_001620_27074ms.jpg` 3（仍在去蒂）→ `kf_001800_30083ms.jpg` 3（仅开始对半切）→ `kf_002160_36099ms.jpg` 1（番茄已离开画面）→ `kf_002520_42116ms.jpg` 4（番茄不在画面，红框落到蛋/背景，属假阳性）。最大红块 `area_frac` 也不单调下降：0ms `0.02030` → 9024ms `0.01519` → 12033ms 反升 `0.01945` → 27074ms `0.00574` → 30083ms 再升 `0.01239` → 36099ms `0.00314` → 42116ms `0.00459`。证据见 `run_real_test2_v4/probe_seg/metrics.jsonl` 与对应 `seg_*.jpg`。
- 采用的 mostly_chopped 阈值(count / largest_frac): **未采用。** 两个必要趋势均不成立，不能从该录像实测出可靠阈值；handoff 中占位的 `count ≥ 6 / largest ≤ 0.008` 也会把 0ms 的整番茄（count 8）误判为切散。
- PREVIEW 相对 CHOP/STEP DONE 的时序: **未执行。** 按第 0 步硬门槛要求，没有实现 `chop_progress`、`--seg` 或 SOP 证据规则，也没有运行 `chop_timing_v1`。
- 预告时刻关键帧名 + 案板状态描述: **未执行。** 基线 audible v2 的旧 PREVIEW 在 `kf_001080_18049ms.jpg`，画面仍是 4 个完整去皮番茄；该帧仅作为失败基线，不是 chop 接线结果。
- wall_seconds 对比 v4: **未执行 chop 回归。** 仅记录前置 audible v2 为 359.84s，v4 为 308.49s；两者不含 FastSAM 主流程接线，不能用于 chop 性能结论。离线探针首帧（含初始化）1949ms，后续多数 51–88ms，个别 122/181ms。
- 遗留问题: FastSAM 的 proposal 数不是番茄块数：同一整颗番茄会产生多个重叠 mask，番茄移出画面后蛋/背景又会被 HSV 误归为 `tomato_red`；并且 `test2.mov` 的 keyframe 在去蒂/对半切后直接切到打蛋，没有拍到“案板上番茄切散成小块”的可验证阶段。需要明天完整视频覆盖实际切块过程后，先原样重跑探针；在双重趋势成立前仍不得接线。
