# 自采数据说明

## 录制目标

所有视频使用头戴式第一人称机位，镜头向下俯视工作区，确保双手、主要容器、案板和锅具持续入镜。每个场景录制 2–3 分钟；不要把包含隐私或无关人物的素材纳入数据集。

## 录制矩阵

| 场景标签 | 场景 | 录制要点 |
|---|---|---|
| `normal-light` | 正常光 | 均匀厨房照明下完成拿起、使用、放下物体的完整动作。 |
| `backlight` | 背光 | 让工作区处于明显逆光，但仍保留完整手物交互。 |
| `steam` | 蒸汽 | 在安全距离内记录蒸汽短时遮挡和恢复后的动作。 |
| `oil-smoke` | 油烟 | 在通风和消防安全前提下记录轻度油烟干扰。 |
| `occlusion` | 遮挡 | 用手臂、锅沿或容器造成部分遮挡，保留遮挡前后状态。 |
| `empty-hand` | 空手 | 记录手靠近、经过物体但不拿取的负样本。 |
| `gloves` | 戴手套 | 使用常见厨房手套完成拿起、握持和放下动作。 |
| `bottle-shapes` | 不同瓶型 | 覆盖透明、深色、细长和宽瓶身等不同瓶型。 |

建议每个场景至少录制一段；若同一场景需要多次录制，在场景标签后追加序号，例如 `20260723_backlight-02.mp4`。

## 文件命名与元数据

- 视频文件：`YYYYMMDD_scene-tag.mp4`。
- 每个视频旁挂同名元数据文件：`YYYYMMDD_scene-tag.meta.json`。
- 元数据至少记录采集设备、分辨率和 SOP 版本；建议同时记录帧率、机位、时长、场景标签和备注。

示例：

```json
{
  "video": "20260723_normal-light.mp4",
  "device": "DFRobot DFR1154",
  "resolution": "1920x1080",
  "fps": 30,
  "camera_position": "head-mounted first-person, top-down workspace",
  "sop_version": "fried-rice-v1",
  "scene": "normal-light",
  "duration_seconds": 150,
  "notes": ""
}
```

视频、抽帧和 session 日志默认不进入 Git；只有经过筛选且版权明确的少量测试 fixture 才可单独评审后入库。
