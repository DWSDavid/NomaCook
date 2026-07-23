"""Context contracts that sit between the state engine and fast perception."""

from .context import (
    ContextDetection,
    ContextualVocabularyController,
    DetectionContext,
    DetectionTarget,
    build_detection_context,
    canonicalize_detections,
)

__all__ = [
    "ContextDetection",
    "ContextualVocabularyController",
    "DetectionContext",
    "DetectionTarget",
    "build_detection_context",
    "canonicalize_detections",
]
