"""The forgetting curve. Pure functions; every constant lives in CurveParams.

R(t) = (1 + t / (9·S))^-1, so R is 0.9 when t equals S. Importance only sets the starting S. Only real use
strengthens a memory, and the gain is larger the more it had been forgotten:
S' = S · (1 + w·α·S^-β·(e^{γ(1−R)} − 1)), and S never shrinks.
"""

import math
from dataclasses import dataclass, field

DAY = 86400.0


@dataclass(frozen=True)
class CurveParams:
    s_min_days: float = 0.5  # importance 1
    s_max_days: float = 30.0  # importance 10
    alpha: float = 3.0
    beta: float = 0.15
    gamma: float = 1.0
    s_cap_days: float = 36500.0
    archive_r: float = 0.30
    labile_factor: float = 0.05  # remembered during the day, not yet consolidated by sleep
    insight_factor: float = 0.5
    level_mult: dict[str, float] = field(default_factory=lambda: {"day": 1.5, "week": 3.0, "month": 6.0})
    weights: dict[str, float] = field(default_factory=lambda: {"use": 1.0, "reencounter": 0.6, "exposure": 0.0, "relearn": 1.0, "parent": 0.3})
    # Recall thresholds, calibrated for perplexity/pplx-embed-v1-4b at 1024 dimensions
    # (unrelated pairs: median cos 0.12, p95 0.27; matches: median 0.45).
    cue_base: float = 0.30
    cue_span: float = 0.35
    tip_rel: float = 0.45
    tip_archived_rel: float = 0.55


DEFAULT = CurveParams()


def retrievability(elapsed_days: float, stability_days: float) -> float:
    if elapsed_days <= 0:
        return 1.0
    return 1.0 / (1.0 + elapsed_days / (9.0 * max(stability_days, 1e-6)))


def r_at(now: float, last_review_at: float, stability_days: float) -> float:
    return retrievability((now - last_review_at) / DAY, stability_days)


def initial_stability(importance: int, p: CurveParams = DEFAULT) -> float:
    """Log-linear from s_min (importance 1) to s_max (importance 10)."""
    imp = min(max(int(importance), 1), 10)
    return p.s_min_days * (p.s_max_days / p.s_min_days) ** ((imp - 1) / 9)


def reinforce(stability_days: float, r: float, kind: str, p: CurveParams = DEFAULT) -> float:
    w = p.weights.get(kind, 0.0)
    gain = w * p.alpha * stability_days ** -p.beta * (math.exp(p.gamma * (1.0 - r)) - 1.0)
    return min(max(stability_days * (1.0 + gain), stability_days), p.s_cap_days)


def days_until(r_target: float, stability_days: float) -> float:
    """Inverse of retrievability: days until R falls to r_target."""
    return 9.0 * stability_days * (1.0 / r_target - 1.0)


def cue_threshold(r: float, p: CurveParams = DEFAULT) -> float:
    """A faded memory needs a stronger cue to come to mind."""
    return p.cue_base + p.cue_span * (1.0 - r)


def recall_score(rel: float, r: float, importance: int) -> float:
    return rel * math.sqrt(max(r, 0.0)) * (0.8 + 0.04 * importance)
