from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class HermesDecision(str, Enum):
    ALLOW = "allow"
    ALLOW_WITH_ADJUSTMENTS = "allow_with_adjustments"
    ASK_PERMISSION = "ask_permission"
    DECLINE = "decline"
    BLOCK = "block"


class HermesFinding(BaseModel):
    type: str
    severity: str = "info"
    label: str
    preview: Optional[str] = None


class HermesRequestContext(BaseModel):
    message: str = ""
    session_id: Optional[str] = None
    mode: str = "chat"
    endpoint_url: Optional[str] = None
    model: Optional[str] = None
    private_mode: bool = False
    use_web: bool = False
    use_research: bool = False
    allow_web_search: bool = False
    allow_bash: bool = False
    attachments: List[str] = Field(default_factory=list)


class HermesPolicyResult(BaseModel):
    decision: HermesDecision
    reason: str
    actions: List[str] = Field(default_factory=list)
    findings: List[HermesFinding] = Field(default_factory=list)
    adjusted_context: Dict[str, Any] = Field(default_factory=dict)
    requires_user_permission: bool = False
    content_visible_to_hermes: bool = True
