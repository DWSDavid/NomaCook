# 番茄炒鸡蛋 — 每一步的阐述与系统上下文(核心 Demo 契约)

> 2026-07-23 · 与代码逐字对齐(`sop/tomato_egg.json` + `server/perception/context.py`
> + `server/pipeline/evidence.py` + `server/vlm/client.py` + `server/pipeline/narrate.py`)。
> 本文回答一个问题:**做这道菜的每一步,系统"知道什么、找什么、听什么、说什么、凭什么放行"。**

## 0. 每一步共用的上下文机器

任何一步激活时,系统同时持有五份上下文:

1. **YOLO 动态词表**:由该步 `objects_involved` 生成,分三色:
   primary(本步目标物)/ anchor(手,常驻)/ confuser(易混淆物,防误判)。
   词表外的东西一律不看(小词表=低误检,这是刻意设计)。
2. **完成判据 `completion_check`**:只写**静态可判定**的结果状态,绝不写动作。
   它就是发给 VLM 的问题本体。
3. **证据打分表**:阈值统一 0.7,需**连续 2 个事件**维持达标才放行;
   分数卡在 `question_min_score` 与 0.7 之间超时 → 系统开口提问。
4. **VLM 请求包**(触发时):系统提示词(低频视觉确认器,只看图、看不清就降
   confidence、不许按菜谱常识脑补)+ `decision_id/step_id/context_version/frame_id`
   (四者必须原样回传,任一不符即判 stale 丢弃)+ 静态完成条件 + 相关物体表 + 当前关键帧。
5. **配音台词槽**:开场 / 步骤完成 / 卡步提问 / 全部完成,四种时机,事件驱动,绝不碎碎念。

通用参数:关键帧采样 3s;VLM TTL 8s、同步骤最小间隔 10s;口头确认需绑定
transcript,高风险步骤还必须绑定提问事件(防"随口嗯一声就过")。

---

## Step 1 · 备料(step_01_prepare)

**指令(开场播报)**:「开始制作番茄炒鸡蛋。第一步,番茄切成小块;鸡蛋打入碗中,加少量盐并搅匀。」

**YOLO 在找什么**(6 概念 9 个 prompt):

| 角色 | 概念 | prompts | 最低置信度 |
|---|---|---|---|
| primary | tomato | tomato | 0.16 |
| primary | egg | chicken egg / egg | 0.16 |
| primary | bowl | mixing bowl / bowl | 0.16 |
| primary | cutting_board | cutting board | 0.16 |
| primary | kitchen_knife | kitchen knife / chef knife | 0.18 |
| anchor | hand | human hand | 0.25 |

**问 VLM 的判据**:「番茄已切成块,碗中蛋液颜色均匀且没有明显完整蛋黄。」
(failure_mode 已写进 SOP:只看到完整番茄和鸡蛋 ≠ 备料完成)

**放行数学**(阈值 0.7,连续 2 次):

| 证据 | 事件类型 | 条件 | 权重 |
|---|---|---|---|
| 料齐了 | perception.objects_present | 同一关键帧内 tomato+egg+bowl 全部可见,`state=tomato_egg_tools_ready`,conf≥0.65 | +0.3 |
| VLM 判完成 | vlm.step_assessment | `phase=likely_complete`,conf≥0.75 | +0.4 |
| 口头确认 | voice.user_confirmation | 需绑定 transcript | +0.3 |

典型放行路径:料齐(0.3)→ 分数进入触发带 → VLM 被唤醒判完成(+0.4)= 0.7
→ 下个关键帧二连达标 → **不需要人工确认自动过**。

**卡步提问**(分数 0.3-0.7 停留 20 秒):「番茄切好、鸡蛋也打匀了吗?」

---

## Step 2 · 炒蛋(step_02_scramble_egg)⚠️ 高风险步骤

**播报**:「这一步完成了。下一步,热锅加油,倒入蛋液,翻炒至鸡蛋凝固成浅黄色块状后盛出。」

**YOLO 在找什么**(9 概念,本步开始出现 confuser):

| 角色 | 概念 | prompts | 最低置信度 |
|---|---|---|---|
| primary | wok | wok / frying pan | 0.16 |
| primary | oil_bottle | cooking oil bottle / oil bottle | 0.15 |
| primary | egg / bowl / spatula / plate | (同表) | 0.15-0.18 |
| anchor | hand | human hand | 0.25 |
| confuser | soy_sauce_bottle | soy sauce bottle / dark condiment bottle | 0.15 |
| confuser | vinegar_bottle | vinegar bottle | 0.16 |

(confuser 的作用:用户伸手拿瓶子时,把"酱油瓶/醋瓶"显式列进词表,
避免 YOLO 把它们框成油瓶导致误证据。)

**颜色信号入场**:检测到 wok 后锁定锅 ROI,HSV 统计。
`yellow_dominant` 定义:黄像素占比 ≥12% 且红 <5%,confidence = yellow/0.30 封顶 1.0。

**问 VLM 的判据**:「鸡蛋已凝固成浅黄色块状并盛出,没有明显液态蛋液或大面积焦褐色。」
(failure_modes:仍有液态蛋液 → 继续加热;出现焦褐 → 提示关小火)

**放行数学**:

| 证据 | 事件类型 | 条件 | 权重 |
|---|---|---|---|
| 锅内变黄 | perception.roi_color | `state=yellow_dominant`,conf≥0.6 | +0.3 |
| VLM 判完成 | vlm.step_assessment | likely_complete,conf≥0.75 | +0.4 |
| 口头确认 | voice.user_confirmation | **高风险:必须绑定系统提问** | +0.3 |

**卡步提问**(20 秒):「鸡蛋已经凝固成块并盛出来了吗?」

---

## Step 3 · 炒软番茄(step_03_soften_tomato)⚠️ 高风险步骤

**播报**:「这一步完成了。下一步,把番茄块倒入锅中,翻炒并轻压,直到番茄明显软化出汁。」

**YOLO 在找什么**(4 概念,全程最小词表):tomato / wok / spatula(primary)+ hand(anchor)。

**颜色信号**:`red_dominant` = 红 ≥12% 且黄 <5%(HSV 双段红区 0-12 + 168-179)。

**问 VLM 的判据**:「锅中番茄边缘已软化并出现可见汤汁,仍保留部分红色块状结构。」
(failure_mode:只有干燥完整番茄块 ≠ 软化)

**放行数学**(注意本步权重刻意调低颜色、调高 VLM,因为"红"不等于"软"):

| 证据 | 事件类型 | 条件 | 权重 |
|---|---|---|---|
| 锅内变红 | perception.roi_color | `state=red_dominant`,conf≥0.6 | **+0.2** |
| VLM 判完成 | vlm.step_assessment | likely_complete,conf≥0.75 | **+0.5** |
| 口头确认 | voice.user_confirmation | 高风险,须绑提问 | +0.3 |

**卡步提问**(本步放宽到 25 秒,触发带下限降到 0.2,因为炖炒过程证据天然稀疏):
「番茄已经炒软并开始出汁了吗?」

---

## Step 4 · 合炒装盘(step_04_combine_and_plate)⚠️ 高风险步骤

**播报**:「这一步完成了。下一步,把鸡蛋倒回锅中,加盐和可选的糖,翻炒均匀后关火盛盘。」

**YOLO 在找什么**(8 概念,全程最大词表):tomato / egg / wok / spatula / salt /
plate / scallion(primary)+ hand(anchor)。

**颜色信号**:`red_yellow_mixed` = 红 ≥6% 且黄 ≥6%,confidence = (红+黄)/0.35。

**问 VLM 的判据**:「红色番茄和黄色蛋块混合均匀并已盛入盘中,灶上不再继续加热。」
(failure_mode:食物仍在锅中加热 ≠ 最终完成——这条是防"提前庆祝"的关键)

**放行数学**(唯一有 4 条规则的步骤):

| 证据 | 事件类型 | 条件 | 权重 |
|---|---|---|---|
| 红黄混合 | perception.roi_color | `state=red_yellow_mixed`,conf≥0.6 | +0.3 |
| VLM 判完成 | vlm.step_assessment | likely_complete,conf≥0.75 | +0.4 |
| 已经上盘 | perception.objects_present | 同帧 plate+wok 可见,`state=food_on_plate`,conf≥0.7 | +0.3 |
| 口头确认 | voice.user_confirmation | 高风险,须绑提问 | +0.3 |

**卡步提问**(20 秒):「番茄和鸡蛋已经拌匀、关火并盛盘了吗?」

**完成播报**:「全部步骤完成,可以盛盘上桌了。妈,我会做饭了。」

---

## 附:为什么这套上下文"抄不走"

单看任何一条信号都平平无奇;壁垒在于每一步的**多信号交集**:词表按步收缩
(误检率随词表大小下降)× 颜色只在锁定的锅 ROI 里比(人走动不影响)×
VLM 只答封闭问题且四个 ID 对不上就作废(幻觉进不了状态机)× 高风险步骤的
确认必须绑定提问(随口应付无效)。误判率是相乘关系,每加一层就降一个量级,
而全系统零自训模型。
