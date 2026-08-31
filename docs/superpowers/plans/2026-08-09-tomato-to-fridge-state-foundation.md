# Tomato-to-Fridge 状态基础实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标：** 在不连接真实 CV、Qwen、Redis 或 VLM 的情况下，用确定性的 event fixture 证明 tomato-to-fridge task graph、recovery、snapshot 和 runtime 写权限边界正确。

**架构：** 复用现有 `EventEnvelope`、`EventLog`、`RecipeSOP` 和 `StateEngine`。先给现有 SOP 增加显式 transition/recovery contract，再由 StateEngine 消费 synthetic event，最后把 `SessionContext` 投影成面向 realtime voice 和 memory 的 `TaskSnapshot`。

**Tech Stack:** Python 3.11、Pydantic 2、pytest 9、JSON SOP、JSONL event fixture。

## Global Constraints

- 只有 `StateEngine` 可以改变 task state。
- 单帧 detection 不能完成关键 transition。
- 关键 transition 至少需要两个独立 evidence source。
- duplicate、out-of-order、stale 和 `shadow=true` event 不能推进 live state。
- 所有 fixture 必须 deterministic；相同 event stream 必须产生相同 snapshot sequence。
- 本计划不连接真实 detector、Qwen、Redis、FastAPI 或远程 VLM。
- 保持现有 fried-rice 和 tomato-egg tests 兼容。
- 所有命令使用 `.venv/bin/python -m pytest`。

---

### Task 1：冻结 tomato-to-fridge SOP contract

**Files:**

- Create: `sop/tomato_to_fridge.json`
- Create: `tests/test_tomato_to_fridge_contract.py`
- Modify: `server/engine/sop.py`

**Interfaces:**

- Consumes: `RecipeSOP.model_validate_json(raw: str) -> RecipeSOP`
- Produces: `RecipeStep.next_step_id: str | None`、`RecipeStep.recovery_transitions: tuple[RecoveryTransition, ...]`

- [ ] **Step 1: 写失败测试，锁定状态和 recovery edge**

```python
from pathlib import Path

from server.engine.sop import load_recipe


def test_tomato_to_fridge_contract_has_expected_graph() -> None:
    recipe = load_recipe(Path("sop/tomato_to_fridge.json"))

    assert recipe.recipe_version_id == "tomato_to_fridge_v1"
    assert [step.id for step in recipe.steps] == [
        "ready",
        "tomato_on_table",
        "hand_near_tomato",
        "tomato_held",
        "tomato_in_transit",
        "fridge_interaction",
        "candidate_inside_fridge",
        "tomato_released_inside",
    ]
    held = next(step for step in recipe.steps if step.id == "tomato_held")
    assert held.next_step_id == "tomato_in_transit"
    assert {
        (edge.event_type, edge.target_step_id)
        for edge in held.recovery_transitions
    } == {("OBJECT_RETURNED_TO_REGION", "tomato_on_table")}
```

- [ ] **Step 2: 运行测试并确认因缺少字段或文件而失败**

Run:

```bash
.venv/bin/python -m pytest tests/test_tomato_to_fridge_contract.py -v
```

Expected: FAIL，因为 `RecoveryTransition`、`next_step_id` 或 SOP fixture 尚不存在。

- [ ] **Step 3: 给 SOP schema 增加最小 graph 字段**

在 `server/engine/sop.py` 增加：

```python
class RecoveryTransition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_type: str = Field(min_length=1)
    payload_matches: dict[str, Any] = Field(default_factory=dict)
    target_step_id: str = Field(min_length=1)


class RecipeStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    title: str = ""
    instruction: str = Field(min_length=1)
    completion_message: str | None = None
    objects_involved: tuple[str, ...] = ()
    completion_check: str = Field(min_length=1)
    est_duration_sec: int = Field(ge=1)
    check_policy: Literal[
        "continuous_evidence",
        "timer_then_visual",
        "visual_then_confirm",
        "user_confirm",
    ]
    tips: tuple[str, ...] = ()
    failure_modes: tuple[str, ...] = ()
    high_risk: bool = False
    completion_policy: CompletionPolicy
    next_step_id: str | None = None
    recovery_transitions: tuple[RecoveryTransition, ...] = ()
```

同时给现有 policy model 增加两个向后兼容字段：

```python
class EvidenceRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    payload_matches: dict[str, Any] = Field(default_factory=dict)
    weight: float = Field(gt=0.0, le=1.0)
    min_confidence: float = Field(ge=0.0, le=1.0)
    advances_confirmation_streak: bool = True
    source_group: str = Field(default="default", min_length=1)


class CompletionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    threshold: float = Field(ge=0.0, le=1.0)
    consecutive_hits: int = Field(ge=1)
    question_min_score: float = Field(ge=0.0, le=1.0)
    question_after_ms: int = Field(ge=0)
    question: str = Field(min_length=1)
    evidence_rules: tuple[EvidenceRule, ...] = Field(min_length=1)
    min_source_groups: int = Field(default=1, ge=1)
    evidence_window_ms: int = Field(default=5_000, ge=100)
```

在 `CompletionPolicy.validate_policy()` 中确认 `min_source_groups` 不大于 policy 中不同 `source_group` 的数量。在 `RecipeSOP.validate_steps()` 中校验所有 `next_step_id` 和 `target_step_id` 都存在于同一份 SOP。旧 SOP 没有显式 `next_step_id` 时继续按 sequence 前进。

- [ ] **Step 4: 创建完整 tomato SOP**

`sop/tomato_to_fridge.json` 必须包含 8 个步骤、以下 active objects 和 recovery：

```json
{
  "schema_version": "1.0",
  "recipe_version_id": "tomato_to_fridge_v1",
  "dish": "把番茄放进冰箱",
  "language": "zh-CN",
  "ingredients": [
    {"name": "tomato", "amount": "1个"}
  ],
  "steps": [
    {
      "id": "ready",
      "sequence": 1,
      "title": "开始",
      "instruction": "请拿起桌上的番茄，把它放进冰箱。",
      "objects_involved": ["tomato", "table", "refrigerator"],
      "completion_check": "番茄和冰箱已经被可靠定位。",
      "est_duration_sec": 5,
      "check_policy": "continuous_evidence",
      "failure_modes": ["番茄不可见", "冰箱不可见"],
      "completion_policy": {
        "threshold": 0.8,
        "consecutive_hits": 2,
        "question_min_score": 0.4,
        "question_after_ms": 3000,
        "question": "我还没同时看到番茄和冰箱，它们现在都在画面里吗？",
        "evidence_rules": [
          {
            "id": "tomato_visible",
            "event_type": "OBJECT_PRESENT",
            "payload_matches": {"object": "tomato"},
            "weight": 0.4,
            "min_confidence": 0.65,
            "advances_confirmation_streak": false
          },
          {
            "id": "fridge_visible",
            "event_type": "DESTINATION_PRESENT",
            "payload_matches": {"region": "refrigerator_interior"},
            "weight": 0.4,
            "min_confidence": 0.65,
            "advances_confirmation_streak": true
          }
        ]
      },
      "next_step_id": "tomato_on_table",
      "recovery_transitions": []
    }
  ]
}
```

其余步骤使用下面的固定定义，不自行增加状态：

| step id | next | completion evidence | source group | recovery |
|---|---|---|---|---|
| `tomato_on_table` | `hand_near_tomato` | `tomato_stable_on_table = OBJECT_STABLE_IN_REGION(tomato, table)` weight 0.8 | `object_region` | 无 |
| `hand_near_tomato` | `tomato_held` | `hand_near = HAND_NEAR_STARTED(tomato)` weight 0.8 | `hand_relation` | `HAND_NEAR_ENDED -> tomato_on_table` |
| `tomato_held` | `tomato_in_transit` | `holding_started = HOLDING_STARTED(tomato)` weight 0.5 + `shared_motion = OBJECT_MOVING_WITH_HAND(tomato)` weight 0.4 | `hand_relation` + `motion` | `OBJECT_RETURNED_TO_REGION(table) -> tomato_on_table` |
| `tomato_in_transit` | `fridge_interaction` | `left_table = OBJECT_LEFT_REGION(tomato, table)` weight 0.4 + `shared_motion = OBJECT_MOVING_WITH_HAND(tomato)` weight 0.5 | `object_region` + `motion` | `OBJECT_RETURNED_TO_REGION(table) -> tomato_on_table` |
| `fridge_interaction` | `candidate_inside_fridge` | `fridge_approach = DESTINATION_INTERACTION(refrigerator)` weight 0.4 + `entered_fridge = OBJECT_ENTERED_REGION(tomato, refrigerator_interior)` weight 0.5 | `destination` + `object_region` | `OBJECT_MOVED_AWAY_FROM_DESTINATION -> tomato_in_transit` |
| `candidate_inside_fridge` | `tomato_released_inside` | `entered_fridge = OBJECT_ENTERED_REGION(tomato, refrigerator_interior)` weight 0.4 + `holding_ended = HOLDING_ENDED(tomato)` weight 0.5 | `object_region` + `hand_relation` | `HOLDING_STARTED(tomato) -> tomato_held` |
| `tomato_released_inside` | session complete | `release_occluded = VISIBILITY_LOST(tomato)` weight 0.4，不推进 streak；`stable_inside = OBJECT_STABLE_IN_REGION(tomato, refrigerator_interior)` weight 0.8 | `visibility` / `region_stability` | `OBJECT_EXITED_REGION(refrigerator_interior) -> tomato_held` |

所有步骤使用 `threshold=0.8`、`consecutive_hits=2`、`question_min_score=0.4`、`question_after_ms=3000` 和 `evidence_window_ms=3000`。包含两个 source group 的中间 transition 设置 `min_source_groups=2`，其余设置为 1。`candidate_inside_fridge -> tomato_released_inside` 已经用 inside-region 与 hand-release 两类证据确认松手；终态再要求 `stable_inside` 连续命中，不能用单帧完成 session。

- [ ] **Step 5: 运行 contract 和现有 engine tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_tomato_to_fridge_contract.py tests/test_state_engine.py -v
```

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add server/engine/sop.py sop/tomato_to_fridge.json tests/test_tomato_to_fridge_contract.py
git commit -m "feat: define tomato-to-fridge task graph"
```

### Task 2：给 event 增加 context 与 runtime mode 边界

**Files:**

- Modify: `server/events/schema.py`
- Modify: `server/pipeline/evidence.py`
- Create: `tests/test_runtime_event_boundaries.py`

**Interfaces:**

- Consumes: existing `create_event(...) -> EventEnvelope`
- Produces: `EventEnvelope.context_version: int | None`、`EventEnvelope.runtime_mode: Literal["RUN", "SHADOW", "REPLAY_EVAL"]`

- [ ] **Step 1: 写失败测试**

```python
from datetime import UTC, datetime

from server.events.schema import EventEnvelope


def test_event_defaults_to_run_and_requires_positive_context_version() -> None:
    event = EventEnvelope(
        event_id="evt_1",
        session_id="session_1",
        seq=1,
        type="OBJECT_PRESENT",
        t_device_ms=100.0,
        t_server_est=datetime.now(UTC),
        received_at=datetime.now(UTC),
        source="fixture",
        confidence=0.9,
        payload={"object": "tomato"},
    )
    assert event.runtime_mode == "RUN"
    assert event.context_version is None
```

再添加一个测试，确认显式传入 `context_version=0` validation fail，并确认现有 evidence factory 会复制调用者传入的 context version。普通 fast-CV event 保持 `None`；只有 VLM response 和与 pending question 绑定的用户确认必须携带 context version。

- [ ] **Step 2: 运行并确认失败**

```bash
.venv/bin/python -m pytest tests/test_runtime_event_boundaries.py -v
```

Expected: FAIL，因为字段尚不存在。

- [ ] **Step 3: 实现 immutable 字段**

在 `EventEnvelope` 增加：

```python
context_version: int | None = Field(default=None, ge=1)
runtime_mode: Literal["RUN", "SHADOW", "REPLAY_EVAL"] = "RUN"
```

更新 `create_event()` 和 `server/pipeline/evidence.py` 的 factory，使它们显式接受并传递这两个字段。不要从 process-global variable 读取 mode。

- [ ] **Step 4: 运行 schema、event log 和 evidence tests**

```bash
.venv/bin/python -m pytest \
  tests/test_runtime_event_boundaries.py \
  tests/test_event_stream.py \
  tests/test_pipeline_evidence.py -v
```

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add server/events/schema.py server/pipeline/evidence.py tests/test_runtime_event_boundaries.py
git commit -m "feat: bind evidence to context and runtime mode"
```

### Task 3：实现 recovery、evidence window 和 runtime 写权限

**Files:**

- Modify: `server/engine/engine.py`
- Modify: `server/engine/models.py`
- Create: `tests/test_task_graph_recovery.py`

**Interfaces:**

- Consumes: `RecoveryTransition`、`EventEnvelope.runtime_mode`、`EventEnvelope.context_version`
- Produces: `EngineResult.status` 新增 `recovered`、`shadow_ignored`、`context_mismatch`；`StepProgress` 增加 evidence window 与 source group

- [ ] **Step 1: 写 shadow 与 stale-context 失败测试**

```python
def test_shadow_event_never_changes_live_context(engine, event_factory) -> None:
    before = engine.context
    result = engine.consume(
        event_factory(
            seq=1,
            event_type="OBJECT_PRESENT",
            runtime_mode="SHADOW",
            context_version=before.context_version,
        )
    )

    assert result.status == "shadow_ignored"
    assert result.context == before


def test_old_context_event_cannot_advance(engine, event_factory) -> None:
    current = engine.context
    result = engine.consume(
        event_factory(
            seq=1,
            event_type="OBJECT_PRESENT",
            runtime_mode="RUN",
            context_version=current.context_version + 1,
        )
    )

    assert result.status == "context_mismatch"
    assert result.context == current
```

普通 fast-CV event 的 `context_version=None` 必须继续被接受；只有显式带版本的 decision-bound event 才做版本相等检查。

- [ ] **Step 2: 写 recovery 失败测试**

先用 fixture event 把 engine 推进到 `tomato_held`，再消费：

```python
recovery = event_factory(
    seq=next_seq,
    event_type="OBJECT_RETURNED_TO_REGION",
    context_version=engine.context.context_version,
    payload={"object": "tomato", "region": "table"},
)
result = engine.consume(recovery)

assert result.status == "recovered"
assert result.context.current_step_id == "tomato_on_table"
assert result.context.step_progress.score == 0.0
```

- [ ] **Step 3: 运行并确认失败**

```bash
.venv/bin/python -m pytest tests/test_task_graph_recovery.py -v
```

Expected: FAIL，因为 engine 仍然只支持线性前进。

- [ ] **Step 4: 写 evidence source 与 expiry 失败测试**

在 test file 中建立 `engine` fixture：加载 tomato SOP，并用合法 fixture event 推进到 `tomato_released_inside`；`event_factory` 使用固定 UTC base time，并根据 `t_device_ms` 生成一致的 `t_server_est` 和 `received_at`。然后增加两个测试：

```python
from server.engine.engine import _completion_ready
from server.engine.sop import CompletionPolicy, EvidenceRule


def test_one_source_group_cannot_satisfy_two_source_policy() -> None:
    rules = (
        EvidenceRule(
            id="hand_a",
            event_type="HAND_A",
            weight=0.4,
            min_confidence=0.8,
            source_group="hand_relation",
        ),
        EvidenceRule(
            id="hand_b",
            event_type="HAND_B",
            weight=0.4,
            min_confidence=0.8,
            source_group="hand_relation",
        ),
        EvidenceRule(
            id="stable_inside",
            event_type="STABLE_INSIDE",
            weight=0.2,
            min_confidence=0.8,
            source_group="region_stability",
        ),
    )
    policy = CompletionPolicy(
        threshold=0.8,
        consecutive_hits=2,
        question_min_score=0.4,
        question_after_ms=3_000,
        question="需要更多证据吗？",
        evidence_rules=rules,
        min_source_groups=2,
        evidence_window_ms=3_000,
    )

    assert not _completion_ready(
        score=0.8,
        consecutive_hits=2,
        matched_source_groups={"hand_relation"},
        policy=policy,
    )
    assert _completion_ready(
        score=1.0,
        consecutive_hits=2,
        matched_source_groups={"hand_relation", "region_stability"},
        policy=policy,
    )


def test_evidence_outside_window_does_not_accumulate(engine, event_factory) -> None:
    first = event_factory(
        seq=1,
        event_type="HOLDING_ENDED",
        t_device_ms=1_000.0,
        confidence=0.95,
        payload={"object": "tomato"},
    )
    late = event_factory(
        seq=2,
        event_type="OBJECT_STABLE_IN_REGION",
        t_device_ms=5_500.0,
        confidence=0.95,
        payload={"object": "tomato", "region": "refrigerator_interior"},
    )

    engine.consume(first)
    result = engine.consume(late)
    assert result.context.step_progress.score == 0.8
    assert result.status != "session_completed"
```

Test fixture 的 `t_server_est` 必须与 `t_device_ms` 使用相同的固定时间偏移，确保第二条 event 超过 3 秒 evidence window。

- [ ] **Step 5: 扩展 StepProgress**

在 `server/engine/models.py` 中增加：

```python
class StepProgress(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    score: float = Field(default=0.0, ge=0.0, le=1.0)
    consecutive_hits: int = Field(default=0, ge=0)
    matched_rule_ids: tuple[str, ...] = ()
    matched_source_groups: tuple[str, ...] = ()
    evidence_refs: tuple[EvidenceReference, ...] = ()
    window_started_at: datetime | None = None
    uncertain_since: datetime | None = None
```

- [ ] **Step 6: 实现最小 recovery 与 evidence-window path**

在 `StateEngine.consume()` 校验 session 后、累计 completion evidence 前，按以下顺序处理：

```python
if event.runtime_mode != "RUN":
    return EngineResult(status="shadow_ignored", context=self._context)
if (
    event.context_version is not None
    and event.context_version != self._context.context_version
):
    return EngineResult(status="context_mismatch", context=self._context)

recovery = next(
    (
        edge
        for edge in self.current_step.recovery_transitions
        if edge.event_type == event.type
        and all(event.payload.get(key) == value for key, value in edge.payload_matches.items())
    ),
    None,
)
if recovery is not None:
    return self._recover_to(event, recovery.target_step_id)
```

`_recover_to()` 根据 recipe step id 找到 index，清空当前 `StepProgress` 和 pending question，增加 context version，并返回 `recovered`。不要删除 event log 中的旧 evidence；回退只改变 snapshot。

在匹配 completion rule 前，如果 `event.t_server_est - progress.window_started_at` 超过 `completion_policy.evidence_window_ms`，先把 progress 重置为 `StepProgress()`。第一条 matched rule 设置 `window_started_at`。每次 matched rule 把自己的 `source_group` 加入 `matched_source_groups`。完成条件同时要求：

```python
score >= step.completion_policy.threshold
and consecutive_hits >= step.completion_policy.consecutive_hits
and len(matched_source_groups) >= step.completion_policy.min_source_groups
```

把这一判断封装为 plan 中测试使用的 `_completion_ready(...)`，`StateEngine.consume()` 也必须调用同一个 helper，避免测试和 production logic 分叉。

- [ ] **Step 7: 用显式 next_step_id 替代固定加一**

在 `_complete_step()` 中：

```python
next_step = self._step_after(completed_step)
```

`_step_after()` 优先使用 `next_step_id`；字段为空时保持现有 sequence 行为，以兼容旧 SOP。

- [ ] **Step 8: 运行 engine regression**

```bash
.venv/bin/python -m pytest \
  tests/test_task_graph_recovery.py \
  tests/test_state_engine.py \
  tests/test_tomato_egg_demo.py -v
```

Expected: PASS。

- [ ] **Step 9: 提交**

```bash
git add server/engine/engine.py server/engine/models.py tests/test_task_graph_recovery.py
git commit -m "feat: add deterministic task recovery"
```

### Task 4：建立对外稳定的 TaskSnapshot

**Files:**

- Create: `server/engine/snapshot.py`
- Create: `tests/test_task_snapshot.py`

**Interfaces:**

- Consumes: `SessionContext`、current `RecipeStep`
- Produces: `build_task_snapshot(context, step) -> TaskSnapshot`

- [ ] **Step 1: 写失败测试**

```python
from server.engine.snapshot import build_task_snapshot


def test_snapshot_is_compact_and_contains_no_write_capability(engine) -> None:
    snapshot = build_task_snapshot(engine.context, engine.current_step)

    assert snapshot.session_id == engine.context.session_id
    assert snapshot.task_id == "tomato_to_fridge_v1"
    assert snapshot.state == "ready"
    assert snapshot.status in {"ON_TRACK", "UNCERTAIN", "COMPLETE"}
    assert snapshot.context_version == engine.context.context_version
    assert not hasattr(snapshot, "advance_step")
    assert "events" not in snapshot.model_dump()
```

- [ ] **Step 2: 运行并确认 import failure**

```bash
.venv/bin/python -m pytest tests/test_task_snapshot.py -v
```

Expected: FAIL，因为 `snapshot.py` 尚不存在。

- [ ] **Step 3: 实现 immutable snapshot model**

```python
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from server.engine.models import SessionContext
from server.engine.sop import RecipeStep


class TaskSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    task_id: str
    state: str
    status: Literal["ON_TRACK", "UNCERTAIN", "DEVIATING", "CRITICAL", "COMPLETE"]
    belief: float = Field(ge=0.0, le=1.0)
    active_objects: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    pending_question: str | None
    last_event_seq: int
    context_version: int


def build_task_snapshot(context: SessionContext, step: RecipeStep) -> TaskSnapshot:
    matched = set(context.step_progress.matched_rule_ids)
    missing = [
        rule.id for rule in step.completion_policy.evidence_rules if rule.id not in matched
    ]
    if (
        context.step_progress.score >= step.completion_policy.threshold
        and context.step_progress.consecutive_hits
        < step.completion_policy.consecutive_hits
    ):
        missing.append("confirmation_streak")
    if context.step_status == "completed":
        status = "COMPLETE"
    elif (
        context.pending_question is not None
        or (
            context.step_progress.score > 0.0
            and (
                context.step_progress.score < step.completion_policy.threshold
                or context.step_progress.consecutive_hits
                < step.completion_policy.consecutive_hits
                or len(context.step_progress.matched_source_groups)
                < step.completion_policy.min_source_groups
            )
        )
    ):
        status = "UNCERTAIN"
    else:
        status = "ON_TRACK"
    return TaskSnapshot(
        session_id=context.session_id,
        task_id=context.recipe_version_id,
        state=context.current_step_id,
        status=status,
        belief=context.step_progress.score,
        active_objects=context.active_objects,
        missing_evidence=tuple(missing),
        pending_question=(
            context.pending_question.question if context.pending_question else None
        ),
        last_event_seq=context.last_seq,
        context_version=context.context_version,
    )
```

- [ ] **Step 4: 测试 JSON round-trip 和字段稳定性**

在同一 test file 中增加 `TaskSnapshot.model_validate_json(snapshot.model_dump_json()) == snapshot`。

- [ ] **Step 5: 运行测试**

```bash
.venv/bin/python -m pytest tests/test_task_snapshot.py tests/test_state_engine.py -v
```

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add server/engine/snapshot.py tests/test_task_snapshot.py
git commit -m "feat: expose immutable task snapshots"
```

### Task 5：建立三条 golden event replay

**Files:**

- Create: `tests/fixtures/tomato_to_fridge/happy_path.jsonl`
- Create: `tests/fixtures/tomato_to_fridge/put_back_on_table.jsonl`
- Create: `tests/fixtures/tomato_to_fridge/occluded_release.jsonl`
- Create: `tests/test_tomato_to_fridge_replay.py`

**Interfaces:**

- Consumes: `read_events(path) -> list[EventEnvelope]`、`StateEngine.consume(event) -> EngineResult`
- Produces: three deterministic final snapshot assertions

- [ ] **Step 1: 写 fixture builder helper**

在 test file 内建立 `replay_fixture(name: str) -> list[TaskSnapshot]`：

```python
def replay_fixture(name: str) -> list[TaskSnapshot]:
    recipe = load_recipe(Path("sop/tomato_to_fridge.json"))
    events = read_events(Path("tests/fixtures/tomato_to_fridge") / name)
    engine = StateEngine(
        session_id=events[0].session_id,
        recipe=recipe,
        started_at=events[0].t_server_est,
    )
    snapshots: list[TaskSnapshot] = []
    for event in events:
        engine.consume(event)
        snapshots.append(build_task_snapshot(engine.context, engine.current_step))
    return snapshots
```

- [ ] **Step 2: 写三个失败测试**

```python
def test_happy_path_reaches_complete() -> None:
    snapshots = replay_fixture("happy_path.jsonl")
    assert snapshots[-1].status == "COMPLETE"


def test_put_back_returns_to_table_without_completion() -> None:
    snapshots = replay_fixture("put_back_on_table.jsonl")
    assert snapshots[-1].state == "tomato_on_table"
    assert snapshots[-1].status != "COMPLETE"


def test_occluded_release_remains_uncertain() -> None:
    snapshots = replay_fixture("occluded_release.jsonl")
    assert snapshots[-1].state == "tomato_released_inside"
    assert snapshots[-1].status == "UNCERTAIN"
    assert "stable_inside" in snapshots[-1].missing_evidence
```

- [ ] **Step 3: 运行并确认 fixture-not-found failure**

```bash
.venv/bin/python -m pytest tests/test_tomato_to_fridge_replay.py -v
```

Expected: FAIL，因为 JSONL fixture 尚不存在。

- [ ] **Step 4: 创建确定性的 JSONL fixture**

规则：

- 全部使用固定 `session_id="session_tomato_fixture"`；
- seq 从 1 连续递增；
- timestamp 使用固定 UTC 时间加 100 ms；
- fast-CV fixture event 使用 `context_version=null`；只有 decision-bound VLM 或用户确认 event 才复制 request 时的 snapshot version；
- happy path 的完成必须包含独立的 `HOLDING_ENDED` 与 `OBJECT_STABLE_IN_REGION`；
- put-back path 在 held state 产生 `OBJECT_RETURNED_TO_REGION`；
- occluded path 产生 `VISIBILITY_LOST`，但缺少 `stable_inside` event，因此 snapshot 保持 uncertain。

- [ ] **Step 5: 重放两次并比较 canonical snapshot JSON**

增加测试：

```python
def test_replay_is_deterministic() -> None:
    first = [item.model_dump_json() for item in replay_fixture("happy_path.jsonl")]
    second = [item.model_dump_json() for item in replay_fixture("happy_path.jsonl")]
    assert first == second
```

- [ ] **Step 6: 运行 foundation suite**

```bash
.venv/bin/python -m pytest \
  tests/test_tomato_to_fridge_contract.py \
  tests/test_runtime_event_boundaries.py \
  tests/test_task_graph_recovery.py \
  tests/test_task_snapshot.py \
  tests/test_tomato_to_fridge_replay.py \
  tests/test_event_stream.py \
  tests/test_state_engine.py -v
```

Expected: PASS。

- [ ] **Step 7: 提交**

```bash
git add tests/fixtures/tomato_to_fridge tests/test_tomato_to_fridge_replay.py
git commit -m "test: lock tomato-to-fridge golden replays"
```

### Task 6：运行 Stage 1 acceptance gate

**Files:**

- Create: `artifacts/tomato_to_fridge/state_foundation_report.json`
- Modify: `.gitignore` only if `artifacts/` is intentionally excluded

**Interfaces:**

- Consumes: all Stage 0–1 tests and three golden fixtures
- Produces: machine-readable acceptance result

- [ ] **Step 1: 运行完整非 e2e test suite**

```bash
.venv/bin/python -m pytest -m "not e2e" -q
```

Expected: PASS with zero failures。

- [ ] **Step 2: 单独运行 deterministic replay 三次**

```bash
for run_id in 1 2 3; do
  .venv/bin/python -m pytest tests/test_tomato_to_fridge_replay.py -q
done
```

Expected: 每次 PASS，snapshot assertions 一致。

- [ ] **Step 3: 写 acceptance report**

文件必须包含：

```json
{
  "stage": "state_foundation",
  "status": "pass",
  "false_completion_fixtures": 0,
  "deterministic_replay_runs": 3,
  "shadow_writes": 0,
  "remote_model_calls": 0
}
```

如果任何测试失败，`status` 必须写为 `fail`，并记录失败测试名称；不能手工写 `pass`。

- [ ] **Step 4: 只提交可复现的 report 或 report generator**

如果 `artifacts/` 在 `.gitignore` 中，则提交生成 report 的 test/helper，不要强行提交 ignored artifact。确认 staged files 后再提交：

```bash
git diff --cached --name-only
git commit -m "test: verify tomato task state foundation"
```

## Stage 1 完成标准

只有同时满足以下条件，才能开始 offline CV implementation plan：

- 全部非 e2e tests 通过；
- 三条 golden replay 结果稳定；
- happy path 正确完成；
- put-back path 正确回退；
- occluded path 保持 uncertain；
- `SHADOW` 和 stale context event 的 live writes 为 0；
- 全过程 remote model calls 为 0。
