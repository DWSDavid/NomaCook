"""Context contracts that sit between the state engine and fast perception."""

from .context import (
    ContextDetection,
    ContextualVocabularyController,
    DetectionContext,
    DetectionTarget,
    build_detection_context,
    canonicalize_detections,
)
from .tomato_egg_signals import (
    TomatoEggColorSignals,
    create_color_evidence_event,
    extract_tomato_egg_color_signals,
)

__all__ = [
    "ContextDetection",
    "ContextualVocabularyController",
    "DetectionContext",
    "DetectionTarget",
    "TomatoEggColorSignals",
    "build_detection_context",
    "canonicalize_detections",
    "create_color_evidence_event",
    "extract_tomato_egg_color_signals",
]
