# Handoff — Phase 1 Live 实测 + 首次 commit

> 给 Codex(或人肉)的执行清单。代码已全部写好并 headless 验证过
> (单测 10/10、检测链路、JSONL、105 FPS 基准),只剩摄像头 live 实测没做。
> 摄像头权限已授权给 Terminal。背景见 docs/PHASE1-GUIDELINE.md。

## 1. Live 实测(唯一未完成的验收项)

```bash
cd ~/Documents/SousAI
.venv/bin/python harness/live_perception.py
```

预期:弹出 "SousAI Layer-1" 窗口,绿色物体框 + 橙色手部骨架 + 左上角 FPS。

**验收动作(对照 PHASE1-GUIDELINE.md §6):**

1. 左上角 FPS ≥ 10(headless 基准 105,live 大概率 30+)。
2. 手里握一个瓶子(bottle)或碗(bowl)在镜头前保持 1-2 秒:
   - 终端打印 `hand_holding_object: Right(或Left) / bottle`
   - 窗口左上角出现 `Right hand holding bottle`
3. 放下物体后 1 秒内终端打印 `hand_holding_object_end`。
4. 按 `q` 退出,记下终端打印的 session 日志路径。
5. 验证日志:`python -c "import json;[json.loads(l) for l in open('<路径>')]"`
   不报错,且文件里能 grep 到 `hand_holding_object`。

**如果 holding 不触发、只出 near:** 是握持阈值问题。把
`perception/hands.py` 里 `is_gripping` 的 `0.55` 下调(先试 0.45),
或看窗口里手部标注的 `grip_closure` 实时数值来定。改完记录最终值。

**如果检测框乱跳:** 属预期(单信号弱证据),只要去抖后事件不高频翻转即可;
翻转频繁就把 `harness/live_perception.py` 里 `InteractionTracker(k_frames=3)` 调到 5。

## 2. 实测通过后:首次 git commit

仓库目前零提交,.gitignore 已备好(weights/、data/sessions/ 等不入库)。

```bash
cd ~/Documents/SousAI
git add -A
git status   # 确认没有 .venv / weights / sessions 混进来
git commit -m "Phase 1: local perception layer (YOLO-World + MediaPipe + fusion + session logging)"
```

## 3. 顺手可做(可选,高杠杆)

- 录一段 2-3 分钟第一人称做菜/备菜视频,存到 `data/test_videos/`,
  然后用 `--source data/test_videos/xxx.mp4` 回放跑一遍,把 session JSONL 留档。
  这是之后所有阈值调参的离线测试集(CLAUDE.md §10 第 1 条)。
- 把实测中调过的阈值(grip、k_frames)写回本文件末尾,方便追溯。

## 实测记录(执行后填写)

- 日期/FPS:
- holding 事件是否闭环(start + end):
- 调整过的阈值:
- session 日志路径:
