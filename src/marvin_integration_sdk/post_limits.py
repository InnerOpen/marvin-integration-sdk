"""What a network allows in a post, declared once by its plugin (0.19.0), so Marvin can check a caption before it goes out.

A provider that posts (a ``social.post`` action) sets ``post_limits`` on its class::

    post_limits = {"max": 2200, "unit": "characters", "visible": 125, "hashtags_max": 30, "links": "not_clickable"}

* ``max`` — the most the network takes (int, required);
* ``unit`` — what ``max`` counts: ``characters`` (default) or ``graphemes`` (what a person sees as one character: an emoji
  with a skin tone is one);
* ``visible`` — how much the feed shows before "… more" (int), or omitted / None when it shows the whole post;
* ``hashtags_max`` — the most hashtags the network allows (int), or omitted when it has no limit;
* ``links`` — what a URL in the text does: ``clickable`` (default), ``not_clickable`` (shown as plain text) or ``card``
  (turned into a link preview).

Marvin core does the checking (the same checks for every network); the plugin only states its network's numbers.
``post_limits_of`` reads and checks a provider's declaration; an unusable one is ignored, never an error.
"""

from __future__ import annotations

UNITS = ("characters", "graphemes")
LINK_BEHAVIOURS = ("clickable", "not_clickable", "card")


def _positive_int(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def check_post_limits(limits) -> list[str]:
    """Why a ``post_limits`` declaration is unusable; empty when it is fine (an empty dict is fine: the provider declares none)."""
    if not isinstance(limits, dict):
        return ["post_limits must be a dict"]
    if not limits:
        return []
    problems = []
    if _positive_int(limits.get("max")) is None:
        problems.append("max must be a positive integer")
    if limits.get("unit", "characters") not in UNITS:
        problems.append(f"unit must be one of {', '.join(UNITS)}")
    for key in ("visible", "hashtags_max"):
        if limits.get(key) is not None and _positive_int(limits.get(key)) is None:
            problems.append(f"{key} must be a positive integer, or left out")
    if limits.get("links", "clickable") not in LINK_BEHAVIOURS:
        problems.append(f"links must be one of {', '.join(LINK_BEHAVIOURS)}")
    return problems


def post_limits_of(provider) -> dict | None:
    """A provider's ``post_limits`` with defaults filled in, or None when it declares none or the declaration is unusable."""
    limits = getattr(provider, "post_limits", None) or {}
    if not limits or check_post_limits(limits):
        return None
    return {
        "max": limits["max"],
        "unit": limits.get("unit", "characters"),
        "visible": limits.get("visible"),
        "hashtags_max": limits.get("hashtags_max"),
        "links": limits.get("links", "clickable"),
    }
