# NomaChef Step 0 执行报告

> 执行日期：2026-07-23
>
> 仓库：`~/Documents/NomaChef`
>
> Python：仓库现有 `.venv`，Python 3.12.13，uv 管理

## 总结

| Phase | 状态 | 结论 |
|---|---|---|
| A：服务端依赖与目录 | 完成 | 依赖安装、import、目录、录制矩阵和精确版本均完成。 |
| B：HowToCook 语料 | 完成 | 下载前已登记；核对 Unlicense；选取 10 道菜并建立索引。 |
| C：API key 模板与注册指引 | 完成 | 空 key 模板、Git 忽略检查和三个注册入口说明已完成；账号注册留给人工。 |
| D：Phase 1 live 实测 | **失败（部分通过）** | 摄像头、FPS 与 JSONL 通过；缺少现场瓶子/碗动作，holding start + end 未闭环。 |
| E：可选研究评估资源 | 完成 | EPIC-SOUNDS 小型标注仓库已放在仓库外；其余只登记，未下载大文件。 |

## Phase A：服务端依赖与目录

状态：完成。

证据与结果：

- 执行 `uv pip install -p .venv fastapi "uvicorn[standard]" websockets python-ulid google-genai supabase`，解析 52 个包、安装 42 个包。
- `.venv/bin/python -c "import fastapi, ulid, google.genai, supabase; print('ok')"` 输出 `ok`；最终复核输出 `imports: ok`。
- `uv pip check -p .venv` 输出 `All installed packages are compatible`。
- 新增直接依赖精确版本：`fastapi==0.139.2`、`uvicorn==0.51.0`、`websockets==15.0.1`、`python-ulid==4.0.1`、`google-genai==2.14.0`、`supabase==2.31.0`。
- 建立 `server/{gateway,engine,events,voice,vlm,knowledge}/`、`sop/corpus/`、`data/test_frames/fixtures/`、`data/annotations/` 并添加 `.gitkeep`。
- `data/README.md` 已记录 8 个录制场景、头戴式第一人称俯视机位、2–3 分钟时长、命名和旁挂元数据规则。
- 提交：`d2d2c4f step0: server deps + dirs`。

## Phase B：HowToCook 语料

状态：完成。

证据与结果：

- 下载前先创建 `docs/DATASET-PLAN.md`，登记来源、日期、License 原文、路径和用途。
- 仓库外克隆路径：`~/datasets/HowToCook`；远端为 `https://github.com/Anduin2017/HowToCook`；固定版本 `be80a97d650713ef5ccbae9514d54db64f926a40`。
- 本地 `LICENSE` 核对为 Unlicense，明确贡献至公有领域并允许商用或非商用复制、修改、发布、使用和分发。
- `sop/corpus/` 含 10 个选定菜谱 Markdown 和 `INDEX.md`；10 个文件均用 `cmp` 验证与上游逐字节一致。
- 选取范围包括蛋炒饭、扬州炒饭、可乐炒饭、蛋包饭、炒方便面和 5 道短流程炒菜；避开长时间炖煮。
- 提交：`ab9c7ab step0: HowToCook corpus (Unlicense)`。

## Phase C：API key 模板与注册指引

状态：完成；真实账号/key 注册跳过并交给人工。

证据与结果：

- `.env.example` 含 5 个空值变量：`GEMINI_API_KEY`、`USDA_FDC_API_KEY`、`SUPABASE_URL`、`SUPABASE_ANON_KEY`、`SUPABASE_SERVICE_ROLE_KEY`。
- `git check-ignore -v .env` 命中 `.gitignore:12:.env`，真实 `.env` 不会被普通 Git 暂存。
- `docs/SETUP-KEYS.md` 已写 Gemini AI Studio、USDA FDC、Supabase 注册步骤、安全注意事项和不打印真实值的本地检查命令。
- 提交：`bf30abc step0: env templates + key setup guide`。

## Phase D：Phase 1 live 实测

状态：失败（部分通过，holding 闭环仍需人工）。

证据与结果：

- `.venv/bin/python harness/live_perception.py` 成功打开摄像头，无权限弹窗；source 0 为唯一可用设备，分辨率 1920×1080，source 1 初始化失败。
- 第二次用同一 live 入口加 `--max-frames 300` 自动收尾：`session_end.t - session_start.t = 18.211s`，端到端平均 `16.47 FPS`，通过 ≥10 FPS 门槛。
- 日志：`data/sessions/2026-07-23T12-10-34_session.jsonl`（已被 Git 忽略）。共 6 行：1 个 `session_start`、4 个 `snapshot`、1 个 `session_end`；逐行 `json.loads` 验证通过。
- snapshot 能看到手，`grip_closure` 约 0.395–0.449，但没有检测到 `bottle` 或 `bowl`，interaction 事件为 0，因此没有 holding start/end。
- `perception/hands.py` 阈值保持 0.55。没有同帧目标物检测，不能把未触发归因于握持阈值，故未修改任何 `perception/`、`harness/` 或 `tests/` 代码。
- 实测结果已追加到 `docs/HANDOFF-live-test.md`。
- 提交：`2ab741b step0: phase-1 live acceptance record`。

## Phase E：可选研究评估资源

状态：完成。

证据与结果：

- 下载前已在 `docs/DATASET-PLAN.md` 登记 EPIC-SOUNDS annotations。
- 仓库外克隆路径：`~/datasets/epic-sounds-annotations`；固定版本 `57a922f0d352e9429f1ef8a37eee21758dd3a33c`；约 32 MB，仅标注。
- 本地 README 核对为 CC BY-NC 4.0；已标记“research-only，永不进产品”，未下载音频或视频。
- CaptainCook4D 官网当前声明数据按 Apache License 2.0 提供；本轮只登记条款和用途，没有下载视频。
- VISOR 登记为 CC BY-NC 4.0，仅限离线研究评估；EPIC-KITCHENS challenge 注册入口已登记，需有效机构邮箱和人工审核。
- OpenDataLab 入口与下厨房语料来源页已登记；因用户生成内容权属不清，未登录、未下载，并明确不得进入产品或 Git。
- 提交：`500ff9f step0: dataset plan registry`。

## 需要人类完成

1. 注册并填写三个本地 key：Gemini、USDA FoodData Central、Supabase；按 `docs/SETUP-KEYS.md` 操作，真实值只放 `.env`。
2. 按 `data/README.md` 录制正常光、背光、蒸汽、油烟、遮挡、空手、戴手套、不同瓶型 8 个自采场景，每段 2–3 分钟，并旁挂 `meta.json`。
3. 从自采视频筛选 20–30 张自有版权关键帧，放入 `data/test_frames/fixtures/`，供 VLM fixture 使用。
4. 重新运行 Phase D：在 source 0 前手持 `bottle` 或 `bowl` 1–2 秒再放下，确认终端和 JSONL 同时出现 `hand_holding_object` 与 `hand_holding_object_end`。若目标物已稳定检测但仍只有 near，再依据窗口中的 `grip_closure` 评估是否把 0.55 下调至 0.45。
5. 如确有研究需要，再用机构邮箱人工注册 EPIC-KITCHENS/VISOR challenge；VISOR 与 EPIC-SOUNDS 仍须保持 research-only。
6. 如确有内部 RAG 研究需要，人工登录 OpenDataLab 查找下厨房语料并先复核授权；未取得明确授权前不要下载或使用。

## 异常与处理

- 工作开始前已存在用户未提交内容：`CLAUDE.md`、`docs/hardware-bom.md`，以及 4 个未跟踪的 NOMACHEF 文档。全程没有 restore、checkout、stash，也没有把它们加入任何提交。
- uv 环境没有 `pip` 模块，首次用 `python -m pip freeze` 查询版本失败；改用 `uv pip freeze -p .venv`，安装和 import 均正常。
- 首次 live 启动下载约 338 MB 的模型辅助权重，完成后摄像头正常打开；OpenCV 窗口没有暴露为可自动控制的 macOS 应用，因此无法替代现场人员执行拿起/放下动作。
- 首次 live 测试用中断信号结束，`finally` 仍成功写入 `session_end` 并关闭日志；第二次用 `--max-frames 300` 正常退出，作为正式 FPS/JSONL 证据。
- source 1 超出可用设备范围；source 0 是唯一可用摄像头，因此保留 source 0。
- CaptainCook4D 官网当前显示 Apache License 2.0，与早期计划中“下载前核对研究用途条款”的保守描述不同；登记表采用当前官网声明，但仍按本项目策略只作可选离线评估。
- 没有运行任何 npm/npx 命令；没有 push、rebase、force、checkout、restore 或 stash。

## Git 提交

1. `d2d2c4f step0: server deps + dirs`
2. `ab9c7ab step0: HowToCook corpus (Unlicense)`
3. `bf30abc step0: env templates + key setup guide`
4. `2ab741b step0: phase-1 live acceptance record`
5. `500ff9f step0: dataset plan registry`
6. `step0: execution report`（本报告提交；hash 在本文件写入后生成，以该提交的 `git log` 为准）

## 最终复核

- `.venv/bin/python -m pytest tests/ -q`：`10 passed in 0.01s`。
- `.venv/bin/python -c "import fastapi, ulid, google.genai, supabase"`：通过。
- `uv pip check -p .venv`：93 个已安装包兼容。
- 本次提交没有修改 `perception/`、`harness/`、`tests/` 下任何现有代码。
