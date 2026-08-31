"""Validated in-memory representation of a frozen recipe SOP."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Ingredient(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    amount: str = Field(min_length=1)


class RecoveryTransition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_type: str = Field(min_length=1)
    payload_matches: dict[str, Any] = Field(default_factory=dict)
    target_step_id: str = Field(min_length=1)


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

    @model_validator(mode="after")
    def validate_policy(self) -> "CompletionPolicy":
        if self.question_min_score >= self.threshold:
            raise ValueError("question_min_score must be below threshold")
        ids = [rule.id for rule in self.evidence_rules]
        if len(ids) != len(set(ids)):
            raise ValueError("evidence rule ids must be unique within a step")
        if sum(rule.weight for rule in self.evidence_rules) < self.threshold:
            raise ValueError("evidence rule weights cannot reach threshold")
        unique_groups = {rule.source_group for rule in self.evidence_rules}
        if self.min_source_groups > len(unique_groups):
            raise ValueError(
                f"min_source_groups ({self.min_source_groups}) exceeds "
                f"distinct source groups ({len(unique_groups)})"
            )
        return self


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


class RecipeSOP(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    recipe_version_id: str = Field(min_length=1)
    dish: str = Field(min_length=1)
    language: str = "zh-CN"
    ingredients: tuple[Ingredient, ...]
    steps: tuple[RecipeStep, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_steps(self) -> "RecipeSOP":
        ids = [step.id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("step ids must be unique")
        sequences = [step.sequence for step in self.steps]
        expected = list(range(1, len(self.steps) + 1))
        if sequences != expected:
            raise ValueError(f"step sequences must be contiguous and ordered: {expected}")
        step_ids = {step.id for step in self.steps}
        for step in self.steps:
            if step.next_step_id is not None and step.next_step_id not in step_ids:
                raise ValueError(
                    f"step {step.id!r} references unknown next_step_id "
                    f"{step.next_step_id!r}"
                )
            for edge in step.recovery_transitions:
                if edge.target_step_id not in step_ids:
                    raise ValueError(
                        f"step {step.id!r} recovery references unknown "
                        f"target_step_id {edge.target_step_id!r}"
                    )
        return self


def load_recipe(path: str | Path) -> RecipeSOP:
    return RecipeSOP.model_validate_json(Path(path).read_text(encoding="utf-8"))
