"""Request/response schemas and controlled vocabularies."""
from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, Field

INTERACTION_TYPES = [
    "one_to_one",          # MSL 1:1 scientific exchange
    "advisory_board",
    "congress",
    "speaker_program",
    "medical_info_request",
    "investigator_meeting",
]
CHANNELS = ["in_person", "virtual", "phone", "email"]
INSIGHT_CATEGORIES = [
    "unmet_need",
    "clinical_practice",
    "treatment_barrier",
    "data_gap",
    "safety_perception",
    "competitive_landscape",
    "access_reimbursement",
    "education_need",
]
OWNER_FUNCTIONS = [
    "Medical Affairs",
    "Medical Education",
    "Clinical Development",
    "HEOR / RWE",
    "Publications",
    "Pharmacovigilance",
    "Medical Information",
    "Market Access",
]


class KOLIn(BaseModel):
    name: str = Field(min_length=1)
    specialty: str
    institution: Optional[str] = None
    region: Optional[str] = None
    therapeutic_area: str
    tier: int = Field(2, ge=1, le=3)
    publications: int = Field(0, ge=0)
    trials_involved: int = Field(0, ge=0)
    email: Optional[str] = None
    notes: Optional[str] = None


class InteractionIn(BaseModel):
    kol_id: int
    date: date
    type: Literal[tuple(INTERACTION_TYPES)]  # type: ignore[valid-type]
    channel: Literal[tuple(CHANNELS)] = "in_person"  # type: ignore[valid-type]
    msl_owner: Optional[str] = None
    topics: Optional[str] = None
    notes: str = ""
    sentiment: Optional[float] = Field(None, ge=-1, le=1)
    duration_min: Optional[int] = Field(None, ge=0)


class InsightIn(BaseModel):
    kol_id: Optional[int] = None
    interaction_id: Optional[int] = None
    category: Literal[tuple(INSIGHT_CATEGORIES)]  # type: ignore[valid-type]
    therapeutic_area: Optional[str] = None
    text: str = Field(min_length=1)


class ActionIn(BaseModel):
    title: str = Field(min_length=1)
    rationale: Optional[str] = None
    owner_function: str = "Medical Affairs"
    priority: Literal["low", "medium", "high"] = "medium"
    status: Literal["open", "in_progress", "done"] = "open"
    due_date: Optional[date] = None
    insight_ids: list[int] = []


class ActionPatch(BaseModel):
    status: Optional[Literal["open", "in_progress", "done"]] = None
    priority: Optional[Literal["low", "medium", "high"]] = None
    due_date: Optional[date] = None


class SynthesisRequest(BaseModel):
    therapeutic_area: Optional[str] = None
    kol_id: Optional[int] = None
    since: Optional[date] = None
    max_interactions: int = Field(60, ge=1, le=300)
    use_ai: bool = True


# ---- AI synthesis output schema (also used as the Claude structured-output format) ----

class Theme(BaseModel):
    title: str
    category: str
    summary: str
    supporting_interaction_ids: list[int]
    kol_count: int


class ActionableOutcome(BaseModel):
    title: str
    rationale: str
    owner_function: str
    priority: Literal["low", "medium", "high"]
    supporting_interaction_ids: list[int]


class ComplianceFlag(BaseModel):
    interaction_id: int
    flag_type: Literal["potential_adverse_event", "off_label_discussion", "promotional_language"]
    excerpt: str
    recommended_step: str


class SynthesisResult(BaseModel):
    executive_summary: str
    themes: list[Theme]
    actionable_outcomes: list[ActionableOutcome]
    compliance_flags: list[ComplianceFlag]
    knowledge_gaps: list[str]
