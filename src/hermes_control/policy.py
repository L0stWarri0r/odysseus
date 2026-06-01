from __future__ import annotations

from .models import HermesDecision, HermesPolicyResult, HermesRequestContext
from .privacy import find_privacy_signals
from .routing import is_local_endpoint


def _local_web_adjustments(ctx: HermesRequestContext) -> tuple[list[str], dict]:
    actions: list[str] = []
    adjusted: dict = {}
    if ctx.use_web:
        actions.append("disable_web")
        adjusted["use_web"] = False
    if ctx.use_research:
        actions.append("disable_research")
        adjusted["use_research"] = False
    if ctx.allow_web_search:
        actions.append("disable_web_search")
        adjusted["allow_web_search"] = False
    return actions, adjusted


def evaluate(ctx: HermesRequestContext) -> HermesPolicyResult:
    """Evaluate a request using Hermes-style control policy.

    Private/local mode is intentionally opaque: when the selected endpoint is
    local and private_mode is enabled, Hermes must not inspect prompt or output
    content. Only metadata-level routing/tool toggles are evaluated.
    """
    local_endpoint = is_local_endpoint(ctx.endpoint_url)

    if ctx.private_mode and local_endpoint:
        actions, adjusted = _local_web_adjustments(ctx)
        if actions:
            return HermesPolicyResult(
                decision=HermesDecision.ALLOW_WITH_ADJUSTMENTS,
                reason="Private local mode keeps content opaque; web/research access was disabled for the local lane.",
                actions=actions,
                adjusted_context=adjusted,
                content_visible_to_hermes=False,
            )
        return HermesPolicyResult(
            decision=HermesDecision.ALLOW,
            reason="Private local mode keeps content opaque to Hermes control.",
            content_visible_to_hermes=False,
        )

    findings = find_privacy_signals(ctx.message)
    if any(f.type == "secret" and f.severity == "critical" for f in findings):
        return HermesPolicyResult(
            decision=HermesDecision.BLOCK,
            reason="Hermes blocked this because it looks like a secret, token, password, private key, or credential.",
            findings=findings,
        )

    if local_endpoint:
        actions, adjusted = _local_web_adjustments(ctx)
        if actions:
            return HermesPolicyResult(
                decision=HermesDecision.ALLOW_WITH_ADJUSTMENTS,
                reason="Local model selected; Hermes disabled web/research access for this local lane.",
                actions=actions,
                findings=findings,
                adjusted_context=adjusted,
            )

    return HermesPolicyResult(
        decision=HermesDecision.ALLOW,
        reason="No Hermes policy intervention needed.",
        findings=findings,
    )
