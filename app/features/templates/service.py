"""Template metadata — port of src/shared/lib/templates.ts."""

TEMPLATE_META: dict[str, dict[str, str]] = {
    "payment": {
        "title": "Payment Request 1",
        "description": "Monthly fee reminder (standard).",
    },
    "payment2": {
        "title": "Payment Request 2",
        "description": "Monthly fee reminder with carried-over sessions.",
    },
    "review_request1": {
        "title": "Review Request 1",
        "description": "For students tutored directly.",
    },
    "review_request2": {
        "title": "Review Request 2",
        "description": "For students tutored through a parent.",
    },
    "recommendation_request1": {
        "title": "Recommendation Request 1",
        "description": "For students tutored directly.",
    },
    "recommendation_request2": {
        "title": "Recommendation Request 2",
        "description": "For students tutored through a parent.",
    },
    "first_approach": {
        "title": "First Approach",
        "description": "Initial outreach to prospective students via Superprof.",
    },
}


def template_meta(id: str) -> dict[str, str]:
    """Return {title, description} for a template id, with safe fallbacks."""
    m = TEMPLATE_META.get(id)
    return {
        "title": m["title"] if m else id,
        "description": m["description"] if m else "",
    }
