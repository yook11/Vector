"""Gemini 実装の Classifier — Stage 2。"""

from __future__ import annotations

import json
from enum import StrEnum

import structlog
from google import genai
from google.genai.errors import APIError, ServerError
from google.genai.types import GenerateContentConfig

from app.analysis.classifier.base import BaseClassifier, ClassificationData
from app.analysis.errors import (
    AnalysisDomainError,
    ConfigurationError,
    InvalidInputError,
    NetworkError,
    ProviderError,
    RateLimitError,
    UnclassifiedError,
)
from app.analysis.extractor.base import EntityData
from app.config import settings
from app.domain.topic import normalize_topic_name
from app.models.article_analysis import ImpactLevel

logger = structlog.get_logger(__name__)


class ValidCategory(StrEnum):
    """LLM 出力の検証に使うカテゴリ slug。"""

    AI = "ai"
    BIO = "bio"
    COMPUTING = "computing"
    ENERGY = "energy"
    MATERIALS = "materials"
    NETWORK = "network"
    ROBOTICS = "robotics"
    SECURITY = "security"
    SEMICONDUCTOR = "semiconductor"
    SPACE = "space"


CLASSIFICATION_PROMPT = """\
You are an expert tech news classifier specializing in emerging technologies.

You will be given a structured summary of a tech news article (already \
translated to Japanese). Based on this summary, classify the article.

You must respond ONLY with a valid JSON object. Do not include markdown \
code fences or any text outside the JSON.

Title: {title_ja}

Summary:
{summary_ja}

Entities:
{entities_section}

Step 1 — Determine the category.
Classify by the article's primary artifact/output domain, NOT by the \
technology used. For example, "AI discovers new material" belongs to \
materials (the output), not ai (the tool).

Select the single most relevant category:
- ai: AI models, services, agents, and AI industry developments.
  Examples: new LLM release, AI startup funding, AI regulation.
  NOT: AI used as a tool in another domain.
- robotics: Autonomous robots, self-driving vehicles, drones, eVTOL.
  Examples: humanoid robot demo, autonomous taxi launch, drone delivery.
  Boundary: If about chips FOR robots → semiconductor.
- semiconductor: Chip design, manufacturing, lithography, packaging.
  Examples: new process node, EUV advancement, chiplet packaging.
  Boundary: If about quantum chips → computing.
- computing: Quantum, neuromorphic, photonic, DNA computing.
  Examples: quantum error correction, neuromorphic chip, optical computing.
- network: 6G, Open RAN, AI-RAN, SDN, submarine cables, DC interconnect.
  Examples: 6G trial, Open RAN deployment, subsea cable project.
- security: PQC, confidential computing, FHE, ZKP, AI security.
  Examples: post-quantum standard, zero-knowledge proof system.
  Boundary: If about cybersecurity incident → only if novel defense tech.
- space: Satellites, rockets, space exploration, orbital infrastructure.
  Examples: rocket launch, satellite constellation, Mars mission.
- bio: Genome editing, gene therapy, synthetic biology, mRNA, AI drug discovery.
  Examples: CRISPR therapy approval, mRNA vaccine, protein structure prediction.
  Boundary: "AI discovers new drug" → bio (the output is the drug).
- materials: Novel materials, 3D printing, nanofabrication.
  Examples: room-temp superconductor, carbon nanotube breakthrough, metamaterials.
  Boundary: "AI discovers new material" → materials.
- energy: Fusion, SMR, next-gen batteries, hydrogen, advanced geothermal.
  Examples: fusion milestone, solid-state battery, green hydrogen plant.

Step 2 — Determine the topic.
Given the category, assign a concise topic label. Rules:
- Lowercase English, 2-4 words, no articles (a/an/the)
- Use established terminology within the category
- Be specific: prefer "euv lithography advancement" over "semiconductor news"
{existing_topics_section}
Step 3 — Assess impact level (provisional).
- low: Incremental update, minor product feature
- medium: Notable development within a specific sector
- high: Significant industry shift, major product launch, large funding round
- critical: Paradigm-changing breakthrough, major regulatory change

Step 4 — Provide reasoning.
Brief explanation in Japanese of why you assigned this category, topic, \
and impact level.

Return a JSON object:
{{
  "category": "one of the 10 category slugs",
  "topic": "concise topic label",
  "impact_level": "low|medium|high|critical",
  "reasoning": "Japanese explanation"
}}
"""


def _build_existing_topics_section(
    topics_by_category: dict[str, list[str]] | None,
) -> str:
    """カテゴリ内の既存 Topic リスト（上位30件）をプロンプトに挿入する。"""
    if not topics_by_category:
        return ""

    lines = [
        "Existing topics by category (use these if applicable, "
        "create a new one only if none fit):"
    ]
    for cat_slug, topics in topics_by_category.items():
        topic_list = ", ".join(f'"{t}"' for t in topics[:30])
        lines.append(f"- {cat_slug}: [{topic_list}]")

    return "\n".join(lines) + "\n"


def _build_entities_section(entities: list[EntityData]) -> str:
    """エンティティリストをプロンプト挿入用テキストに整形する。"""
    if not entities:
        return "(none)"
    return ", ".join(f"{e.name} ({e.type.value})" for e in entities)


class GeminiClassifier(BaseClassifier):
    """BaseClassifier の Gemini API 実装。"""

    MODEL = "gemini-2.5-flash-lite"
    RPM = 50
    RPD = 1500

    def __init__(self) -> None:
        api_key = settings.gemini_api_key.get_secret_value()
        if not api_key:
            raise ConfigurationError("GEMINI_API_KEY is not configured")
        self._client = genai.Client(api_key=api_key)

    async def classify(
        self,
        title_ja: str,
        summary_ja: str,
        entities: list[EntityData],
        existing_topics_by_category: dict[str, list[str]] | None = None,
    ) -> ClassificationData:
        """Stage 1 の出力を分類する。原文は読まない。"""
        entities_section = _build_entities_section(entities)
        existing_topics_section = _build_existing_topics_section(
            existing_topics_by_category,
        )

        prompt = CLASSIFICATION_PROMPT.format(
            title_ja=title_ja,
            summary_ja=summary_ja,
            entities_section=entities_section,
            existing_topics_section=existing_topics_section,
        )

        raw_text = await self._call_once(prompt)
        return self._parse_response(raw_text)

    async def _call_api(self, prompt: str) -> str:
        """Gemini の generate_content API を呼び出す。"""
        response = await self._client.aio.models.generate_content(
            model=self.MODEL,
            contents=prompt,
            config=GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=1024,
            ),
        )
        if response.text is None:
            raise ProviderError("Gemini returned empty response")
        return response.text

    def _translate_error(self, exc: Exception) -> AnalysisDomainError:
        """Gemini SDK の例外を原因の所在で分類する。"""
        if isinstance(exc, APIError):
            status = exc.status or ""
            message = exc.message or ""

            if "reported as leaked" in message:
                return ConfigurationError(f"API key leaked: {message}")

            if status in (
                "UNAUTHENTICATED",
                "PERMISSION_DENIED",
                "FAILED_PRECONDITION",
                "NOT_FOUND",
            ):
                return ConfigurationError(f"{status}: {message}")

            if status in ("INVALID_ARGUMENT", "DEADLINE_EXCEEDED"):
                return InvalidInputError(f"{status}: {message}")

            if status == "RESOURCE_EXHAUSTED":
                return RateLimitError(f"{status}: {message}")

            if isinstance(exc, ServerError):
                return ProviderError(f"{status}: {message}")

            return UnclassifiedError(
                f"Unhandled APIError {exc.code} {status}: {message}"
            )

        if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
            return NetworkError(f"{type(exc).__name__}: {exc}")

        return UnclassifiedError(f"{type(exc).__name__}: {exc}")

    def _parse_response(self, raw_text: str) -> ClassificationData:
        """Gemini からの JSON レスポンスを解析・検証する。"""
        text = raw_text.strip()

        if text.startswith("```"):
            first_newline = text.index("\n")
            text = text[first_newline + 1 :]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            logger.error(
                "classifier_json_parse_error",
                raw_text=raw_text[:500],
                error=str(e),
            )
            raise ProviderError(f"Failed to parse Gemini response as JSON: {e}")

        try:
            category = str(data["category"]).strip().lower()
            try:
                ValidCategory(category)
            except ValueError:
                raise ProviderError(
                    f"Invalid category from Gemini: {category!r}. "
                    f"Expected one of: {[c.value for c in ValidCategory]}"
                )

            raw_topic = str(data["topic"])
            topic_name = normalize_topic_name(raw_topic)

            impact_level = ImpactLevel(data["impact_level"])

            return ClassificationData(
                category_slug=category,
                topic_name=topic_name,
                impact_level=impact_level,
                reasoning=str(data.get("reasoning", "")),
            )
        except (KeyError, TypeError, ValueError) as e:
            logger.error(
                "classifier_validation_error",
                data=data,
                error=str(e),
            )
            raise ProviderError(f"Invalid classification data from Gemini: {e}")
