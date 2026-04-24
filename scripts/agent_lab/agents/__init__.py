"""Forge agents.

Exposes 7 AgentDefinitions:
- PLANNER: Round 1 (drafts v1.md)
- SPECIALISTS: Round 2 (5 parallel contributions)
- SYNTHESIZER: Round 3 (integrates into PLAN.md)
"""

from agents.backend import AGENT as BACKEND
from agents.domain_expert import AGENT as DOMAIN_EXPERT
from agents.frontend import AGENT as FRONTEND
from agents.planner import AGENT as PLANNER
from agents.security import AGENT as SECURITY
from agents.sre import AGENT as SRE
from agents.synthesizer import AGENT as SYNTHESIZER

SPECIALISTS = {
    "domain_expert": DOMAIN_EXPERT,
    "backend": BACKEND,
    "security": SECURITY,
    "sre": SRE,
    "frontend": FRONTEND,
}

ALL_AGENTS = {
    "planner": PLANNER,
    **SPECIALISTS,
    "synthesizer": SYNTHESIZER,
}

__all__ = [
    "ALL_AGENTS",
    "BACKEND",
    "DOMAIN_EXPERT",
    "FRONTEND",
    "PLANNER",
    "SECURITY",
    "SPECIALISTS",
    "SRE",
    "SYNTHESIZER",
]
