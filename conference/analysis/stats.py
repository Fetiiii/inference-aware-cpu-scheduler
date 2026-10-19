"""Pure numerical helpers for the frozen C05 paired analysis protocol.

This module does not read experiment files or select observations. Callers must
establish the registered comparison, complete round pairing, and provenance.
No third-party statistical package or random sampling is used here.
"""

from __future__ import annotations

import itertools
import math
import statistics
from collections.abc import Sequence


def _finite_values(values: Sequence[float], name: str) -> list[float]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ValueError(f"{name} must be a sequence of finite numbers")
    result = []
    for index, value in enumerate(values):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name}[{index}] must be a finite number")
        try:
            number = float(value)
        except (OverflowError, ValueError) as exc:
            raise ValueError(f"{name}[{index}] must be finite") from exc
        if not math.isfinite(number):
            raise ValueError(f"{name}[{index}] must be finite")
        result.append(number)
    return result


def _beta_continued_fraction(a: float, b: float, x: float) -> float:
    """Modified Lentz continued fraction for the regularized beta function."""
    tiny = 1e-300
    eps = 2e-15
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, 10001):
        m2 = 2 * m
        numerator = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + numerator * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + numerator / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        numerator = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + numerator * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + numerator / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        factor = d * c
        h *= factor
        if abs(factor - 1.0) <= eps:
            return h
    raise ArithmeticError("incomplete beta continued fraction did not converge")


def _regularized_beta(x: float, a: float, b: float) -> float:
    if not 0.0 <= x <= 1.0 or a <= 0.0 or b <= 0.0:
        raise ValueError("invalid incomplete beta arguments")
    if x in (0.0, 1.0):
        return x
    scale = math.exp(
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
        + a * math.log(x) + b * math.log1p(-x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        value = scale * _beta_continued_fraction(a, b, x) / a
    else:
        value = 1.0 - scale * _beta_continued_fraction(b, a, 1.0 - x) / b
    return min(1.0, max(0.0, value))


def _student_t_two_sided_p(t: float, df: int) -> float:
    if df < 1 or math.isnan(t):
        raise ValueError("Student t needs positive df and non-NaN t")
    if math.isinf(t):
        return 0.0
    x = df / (df + t * t)
    return _regularized_beta(x, df / 2.0, 0.5)


def _student_t_critical(confidence: float, df: int) -> float:
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("confidence must be a number strictly between 0 and 1")
    if not math.isfinite(confidence) or not 0.0 < confidence < 1.0:
        raise ValueError("confidence must be strictly between 0 and 1")
    if df < 1:
        raise ValueError("Student t needs positive degrees of freedom")
    target_p = 1.0 - confidence
    low, high = 0.0, 1.0
    while _student_t_two_sided_p(high, df) > target_p:
        high *= 2.0
        if not math.isfinite(high):
            raise ArithmeticError("Student t critical value overflowed")
    for _ in range(100):
        middle = (low + high) / 2.0
        if _student_t_two_sided_p(middle, df) > target_p:
            low = middle
        else:
            high = middle
    return (low + high) / 2.0


def paired_analysis(
    a: Sequence[float], b: Sequence[float], confidence: float = 0.95
) -> dict:
    """Analyze matched a-b pairs; no pairing or eligibility inference is done.

    Zero observed difference variance produces a point interval. If its mean
    is nonzero, p=0 is the zero-SE limiting value, not evidence that the
    approximate-normal paired-t model has been validated. The t statistic is
    then None to keep JSON serialization finite and the condition explicit.
    """
    arm_a, arm_b = _finite_values(a, "a"), _finite_values(b, "b")
    if len(arm_a) != len(arm_b):
        raise ValueError("paired arms must have equal lengths")
    n = len(arm_a)
    if n < 2:
        raise ValueError("paired analysis requires at least two complete pairs")
    critical = _student_t_critical(confidence, n - 1)
    differences = [left - right for left, right in zip(arm_a, arm_b)]
    if any(not math.isfinite(value) for value in differences):
        raise ValueError("paired differences overflowed")
    mean_a, mean_b = math.fsum(arm_a) / n, math.fsum(arm_b) / n
    mean_difference = math.fsum(differences) / n
    if not all(math.isfinite(value) for value in (mean_a, mean_b, mean_difference)):
        raise ValueError("derived mean overflowed")
    notes = []
    if mean_b == 0.0:
        relative_pct = None
        relative_reason = "comparator arm mean is zero"
        notes.append(relative_reason)
    else:
        relative_pct = 100.0 * (mean_a / mean_b - 1.0)
        if not math.isfinite(relative_pct):
            raise ValueError("relative percentage overflowed")
        relative_reason = None

    sd_difference = statistics.stdev(differences)
    se_difference = sd_difference / math.sqrt(n)
    if sd_difference == 0.0:
        ci_low = ci_high = mean_difference
        if mean_difference == 0.0:
            t_statistic, p_value = 0.0, 1.0
            notes.append("all paired differences are zero; paired-t variance is degenerate")
        else:
            t_statistic, p_value = None, 0.0
            notes.append(
                "nonzero constant paired difference: zero-SE limiting p=0; "
                "paired-t variance is degenerate"
            )
    else:
        t_statistic = mean_difference / se_difference
        if not math.isfinite(t_statistic):
            raise ValueError("t statistic overflowed")
        p_value = _student_t_two_sided_p(t_statistic, n - 1)
        margin = critical * se_difference
        ci_low, ci_high = mean_difference - margin, mean_difference + margin
        if not all(math.isfinite(value) for value in (ci_low, ci_high)):
            raise ValueError("confidence interval overflowed")
    return {
        "n": n,
        "df": n - 1,
        "mean_a": mean_a,
        "mean_b": mean_b,
        "median_a": statistics.median(arm_a),
        "median_b": statistics.median(arm_b),
        "differences": differences,
        "mean_difference": mean_difference,
        "relative_pct": relative_pct,
        "relative_reason": relative_reason,
        "t_statistic": t_statistic,
        "p_value": p_value,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "sd_difference": sd_difference,
        "se_difference": se_difference,
        "notes": notes,
    }


def holm_adjust(pvalues: Sequence[float], alpha: float = 0.05) -> list[float]:
    """Return Holm adjusted p-values in original order (reject iff <= alpha).

    Equal p-values are ordered stably; the cumulative-maximum step gives each
    equal value the same adjusted p-value. Family membership belongs to caller.
    """
    p = _finite_values(pvalues, "pvalues")
    if not p or any(not 0.0 <= value <= 1.0 for value in p):
        raise ValueError("pvalues must be a nonempty sequence in [0, 1]")
    if isinstance(alpha, bool) or not isinstance(alpha, (int, float)):
        raise ValueError("alpha must be in (0, 1)")
    if not math.isfinite(alpha) or not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be in (0, 1)")
    adjusted = [0.0] * len(p)
    running = 0.0
    for rank, index in enumerate(sorted(range(len(p)), key=lambda i: (p[i], i))):
        running = max(running, min(1.0, (len(p) - rank) * p[index]))
        adjusted[index] = running
    return adjusted


def exact_sign_flip(differences: Sequence[float]) -> dict:
    """Enumerate the frozen n=6, two-sided absolute-mean sign-flip test.

    Conditional exactness needs independent difference sign symmetry under
    the null, or a justified within-pair label-exchangeability model. A zero
    mean alone does not establish this assumption.
    """
    d = _finite_values(differences, "differences")
    if len(d) != 6:
        raise ValueError("C05 sign-flip sensitivity requires exactly six pairs")
    statistic = abs(math.fsum(d) / 6.0)
    tolerance = 100.0 * math.ulp(1.0) * statistic
    if not math.isfinite(statistic) or not math.isfinite(tolerance):
        raise ValueError("sign-flip statistic overflowed")
    extreme = 0
    for signs in itertools.product((-1.0, 1.0), repeat=6):
        flipped = abs(math.fsum(sign * value for sign, value in zip(signs, d)) / 6.0)
        if flipped >= statistic - tolerance:
            extreme += 1
    return {
        "n": 6,
        "patterns": 64,
        "extreme_count": extreme,
        "p_value": extreme / 64.0,
        "statistic": statistic,
        "tie_tolerance": tolerance,
        "assumptions": (
            "Conditional exactness requires independent sign symmetry of paired "
            "differences under the null, or justified within-pair label "
            "exchangeability; zero mean alone is insufficient."
        ),
    }
