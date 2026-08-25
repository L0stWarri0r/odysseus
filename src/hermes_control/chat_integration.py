from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

from .models import HermesDecision, HermesPolicyResult, HermesRequestContext
from .policy import evaluate


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class HermesChatPolicyApplication:
    policy: HermesPolicyResult
    use_web: bool
    use_research: bool
    allow_web_search: bool


def _apply_hermes_control_policy(
    *,
    message: str,
    session_id: str,
    sess,
    mode: str = "chat",
    private_mode: Any = False,
    incognito: Any = False,
    use_web: Any = False,
    use_research: Any = False,
    allow_web_search: Any = False,
) -> HermesChatPolicyApplication:
    """Evaluate and apply Hermes Control chat-routing policy.

    This helper only passes message text to Hermes Control when the request is
    not private/local. In private local mode, `evaluate()` itself guarantees
    opacity and returns no text-derived findings. Incognito / Nobody mode is
    treated as private_mode so the UI toggle actually reaches the opaque lane.
    """
    use_web_bool = _truthy(use_web)
    use_research_bool = _truthy(use_research)
    allow_web_search_bool = _truthy(allow_web_search)
    private_mode_bool = _truthy(private_mode) or _truthy(incognito)

    policy = evaluate(
        HermesRequestContext(
            message=message or "",
            session_id=session_id,
            mode=mode or "chat",
            endpoint_url=getattr(sess, "endpoint_url", None),
            model=getattr(sess, "model", None),
            private_mode=private_mode_bool,
            use_web=use_web_bool,
            use_research=use_research_bool,
            allow_web_search=allow_web_search_bool,
        )
    )

    if policy.decision in {HermesDecision.BLOCK, HermesDecision.DECLINE}:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "HERMES_CONTROL_BLOCKED",
                "message": policy.reason,
                "policy": policy.model_dump(mode="json"),
            },
        )

    adjusted = policy.adjusted_context or {}
    if "use_web" in adjusted:
        use_web_bool = bool(adjusted["use_web"])
    if "use_research" in adjusted:
        use_research_bool = bool(adjusted["use_research"])
    if "allow_web_search" in adjusted:
        allow_web_search_bool = bool(adjusted["allow_web_search"])

    return HermesChatPolicyApplication(
        policy=policy,
        use_web=use_web_bool,
        use_research=use_research_bool,
        allow_web_search=allow_web_search_bool,
    )
