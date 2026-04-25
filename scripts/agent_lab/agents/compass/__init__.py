"""Compass agents.

10 specialists (parallel ideation) + 1 synthesizer (integration).
"""

from agents.compass.ai_architect import AGENT as AI_ARCHITECT
from agents.compass.competitor import AGENT as COMPETITOR
from agents.compass.devils_advocate import AGENT as DEVILS_ADVOCATE
from agents.compass.legal import AGENT as LEGAL
from agents.compass.pm import AGENT as PM
from agents.compass.security import AGENT as SECURITY
from agents.compass.sre import AGENT as SRE
from agents.compass.synthesizer import AGENT as SYNTHESIZER
from agents.compass.tech_lead import AGENT as TECH_LEAD
from agents.compass.tech_scout import AGENT as TECH_SCOUT
from agents.compass.user import AGENT as USER

SPECIALISTS = {
    "user": USER,
    "pm": PM,
    "devils_advocate": DEVILS_ADVOCATE,
    "competitor": COMPETITOR,
    "ai_architect": AI_ARCHITECT,
    "tech_lead": TECH_LEAD,
    "sre": SRE,
    "tech_scout": TECH_SCOUT,
    "security": SECURITY,
    "legal": LEGAL,
}

ALL_AGENTS = {**SPECIALISTS, "synthesizer": SYNTHESIZER}

__all__ = [
    "AI_ARCHITECT",
    "ALL_AGENTS",
    "COMPETITOR",
    "DEVILS_ADVOCATE",
    "LEGAL",
    "PM",
    "SECURITY",
    "SPECIALISTS",
    "SRE",
    "SYNTHESIZER",
    "TECH_LEAD",
    "TECH_SCOUT",
    "USER",
]
