# 数据集与语料登记

> 原则：任何数据集或语料下载前先登记。NC、research-only 或权属不清资源仅可存放在仓库外的 `~/datasets/`，不得进入产品、训练管线或 Git。

| 资源 | 日期 | License | 条款链接 | 路径 | 用途 |
|---|---|---|---|---|---|
| HowToCook | 2026-07-23 | Unlicense；作者将作品贡献至公有领域，允许复制、修改、发布、使用、编译、销售和分发，也不提供任何担保 | [仓库 LICENSE 原文](https://github.com/Anduin2017/HowToCook/blob/master/LICENSE) | `~/datasets/HowToCook`（完整仓库，commit `be80a97d650713ef5ccbae9514d54db64f926a40`）；`sop/corpus/`（选取 10 道菜） | 结构化中文菜谱语料，用于“菜谱 → SOP/状态机”解析与 fixture |
| EPIC-SOUNDS annotations | 2026-07-23 | CC BY-NC 4.0；必须署名、提供许可链接并标注修改，不得商用 | [仓库 License 段](https://github.com/epic-kitchens/epic-sounds-annotations#license)；[CC BY-NC 4.0 原文](https://creativecommons.org/licenses/by-nc/4.0/legalcode.en) | `~/datasets/epic-sounds-annotations`（commit `57a922f0d352e9429f1ef8a37eee21758dd3a33c`，约 32 MB，仅标注） | **research-only，永不进产品**；内部验证厨房声音规则与 YAMNet 召回，不下载原始音视频 |
| CaptainCook4D | 2026-07-23 | 官网声明数据按 Apache License 2.0 提供；本项目仍只作为可选离线评估资源 | [官网 Data 段](https://captaincook4d.github.io/captain-cook/)；[Apache-2.0 原文](https://www.apache.org/licenses/LICENSE-2.0) | 不下载；如后续人工批准，存 `~/datasets/CaptainCook4D` | 仅评估带错误步骤的状态机误推进率，本轮不下载视频 |
| EPIC-KITCHENS VISOR | 2026-07-23 | CC BY-NC 4.0；必须署名、提供许可链接并标注修改，不得商用 | [VISOR Copyright 原文](https://epic-kitchens.github.io/VISOR/site)；[CC BY-NC 4.0 原文](https://creativecommons.org/licenses/by-nc/4.0/legalcode.en) | 不下载；如后续人工批准，存 `~/datasets/VISOR` | **research-only，永不进产品**；手物关系失败案例评估 |
| 下厨房菜谱语料 | 2026-07-23 | 权属不清；语料来自用户上传菜谱，研究数据集页面未提供足以支持商用的明确许可 | [数据集来源说明](https://counterfactual-recipe-generation.github.io/dataset_zh.html) | 本轮不下载；如经人工许可复核，仅存 `~/datasets/xiachufang` | 仅内部 RAG 检索实验候选，永不进入产品语料或 Git |
| Open Images V7 厨房类别子集 | 2026-07-23 | 标注为 CC BY 4.0；图片列为 CC BY 2.0。Google 明确要求逐图自行核验许可状态，因此使用时必须保留原图作者、落地页与 License 元数据 | [官方 License 说明](https://storage.googleapis.com/openimages/web/factsfigures_v7.html#licenses)；[官方按类别子集下载说明](https://storage.googleapis.com/openimages/web/download_v7.html) | 本轮不下载；批准后仅下载小型子集至 `~/datasets/open-images-kitchen` | **产品候选，但需逐图许可核验和署名**；厨房物体检测基线、提示词类别表与独立测试集 |
| AI2-THOR 合成厨房场景 | 2026-07-23 | 代码仓库为 Apache License 2.0；正式使用生成图像前仍需复核所用 Unity 场景和第三方资产清单 | [官方仓库 LICENSE](https://github.com/allenai/ai2thor/blob/main/LICENSE)；[Apache-2.0 原文](https://www.apache.org/licenses/LICENSE-2.0) | 本轮不下载；批准后存 `~/datasets/ai2thor-kitchen` | **合成补充候选**；生成 RGB、深度、实例分割和物体状态真值，覆盖遮挡/光照变化；不可代替真实厨房评估 |

## 待办

后续需人工注册或复核的资源在此登记；任何下载仍需先补全上表中的 License、条款链接、路径和用途。

- [ ] EPIC-KITCHENS / VISOR：从 [EPIC-KITCHENS 2025 challenge 页面](https://epic-kitchens.github.io/2025) 进入对应 Codabench 和团队注册表。官方要求有效的机构邮箱并人工审核；本轮不代注册。VISOR 公共标注下载入口在 [VISOR 官网](https://epic-kitchens.github.io/VISOR/site)，但因 CC BY-NC 4.0 本项目不下载、不进产品。
- [ ] OpenDataLab（下厨房语料）：从 [OpenDataLab 数据集入口](https://opendatalab.com/home) 登录后搜索“下厨房/中文菜谱”。下载前必须由人工核对平台元数据、原作者授权和可用范围；当前来源权属不清，不得商用或进入产品。
