"""AI adapters for question answering."""

from app.agent.answering.ai.gemini import (
    GeminiEvidenceAnswerDraftGenerator,
    GeminiEvidenceAnswerResponseDefect,
    GeminiEvidenceAnswerResponseInvalidError,
)
from app.agent.answering.ai.gemini_direct import GeminiDirectAnswerGenerator
from app.agent.answering.ai.gemini_direct_spec import (
    GEMINI_DIRECT_ANSWER_SPEC,
    GeminiDirectAnswerSpec,
)
from app.agent.answering.ai.gemini_spec import (
    GEMINI_EVIDENCE_ANSWER_SPEC,
    GeminiEvidenceAnswerSpec,
)

__all__ = [
    "GEMINI_DIRECT_ANSWER_SPEC",
    "GEMINI_EVIDENCE_ANSWER_SPEC",
    "GeminiDirectAnswerGenerator",
    "GeminiDirectAnswerSpec",
    "GeminiEvidenceAnswerDraftGenerator",
    "GeminiEvidenceAnswerResponseDefect",
    "GeminiEvidenceAnswerResponseInvalidError",
    "GeminiEvidenceAnswerSpec",
]
