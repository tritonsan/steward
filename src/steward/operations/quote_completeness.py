"""Quote completeness is an application gate, never a model permission."""

from datetime import timedelta


def missing_fields(payload):
    e = payload.get("extraction", {})
    required = {
        "total": e.get("amount"),
        "explicit currency": e.get("currency") and not e.get("currency_inferred_from_rfq", False),
        "scope": e.get("scope_evidence"),
        "inclusions": e.get("inclusions_evidence"),
        "exclusions (or explicit none)": e.get("exclusions_evidence"),
        "availability": e.get("earliest_onsite_at") and e.get("onsite_evidence"),
        "validity": e.get("valid_until") and e.get("validity_evidence"),
    }
    return [name for name, value in required.items() if not value]


def review_due(runtime, case):
    sent = [
        o.delivered_at
        for o in runtime.store.outbox_for_case(case.case_id)
        if o.payload.get("purpose") == "rfq" and o.delivered_at
    ]
    policy = runtime._policy.policy_for(case.category)
    return min(sent) + timedelta(hours=policy.quote_wait_hours if policy else 24) if sent else None
