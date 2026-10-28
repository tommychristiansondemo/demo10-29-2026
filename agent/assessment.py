"""Combined FDA + inventory assessment composition (Requirement 7.4).

When a user asks to compare FDA shortage status against pharmacy inventory,
the agent gathers results from the FDA tools (``search_drug_shortages``,
``get_recalls``, ``find_therapeutic_alternatives``, ``lookup_ndc``) and the
inventory tools (``get_stock_levels``, ``get_low_stock_items``) — each of
which returns the shared structured envelope ``{status, message, data}`` —
and composes them into a **single** combined assessment for the user.

This module holds that composition as pure logic over envelope dicts so it is
testable without the model, the Gateway, or live AWS. The language model
narrates the assessment to the user; this structure gives it (and tests) a
deterministic, uniform object to reason over: a per-drug supply-risk summary
joined to on-hand inventory, with a derived risk level and a short set of
recommended actions (e.g. surface therapeutic alternatives when a drug is in
shortage or recalled).

The composer is intentionally defensive about envelope shape: envelopes may be
``ToolEnvelope`` instances or plain dicts, ``data`` may be missing, and an
``ERROR`` status is carried through so the narrated answer can report a
degraded tool rather than silently dropping it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from shared.envelope import STATUS_EMPTY, STATUS_ERROR, STATUS_OK

# Risk levels, ordered from least to most severe, used to summarize the
# combined supply picture for a drug.
RISK_NONE = "NONE"
RISK_LOW = "LOW"
RISK_MODERATE = "MODERATE"
RISK_HIGH = "HIGH"


def _as_envelope_dict(envelope: Any) -> Dict[str, Any]:
    """Coerce an envelope (dict or ``ToolEnvelope``) into a plain dict.

    Returns a uniform ``{status, message, data}`` dict. Unknown/None inputs
    collapse to an ERROR envelope so downstream code never has to special-case
    ``None``.
    """
    if envelope is None:
        return {"status": STATUS_ERROR, "message": "no result", "data": []}
    # ToolEnvelope exposes ``to_dict``; use it when available.
    to_dict = getattr(envelope, "to_dict", None)
    if callable(to_dict):
        envelope = to_dict()
    if not isinstance(envelope, dict):
        return {"status": STATUS_ERROR, "message": "malformed result", "data": []}
    status = envelope.get("status", STATUS_ERROR)
    # Normalize a Status enum to its string value.
    status = getattr(status, "value", status)
    data = envelope.get("data")
    if not isinstance(data, list):
        data = [] if data is None else [data]
    return {
        "status": status,
        "message": envelope.get("message", ""),
        "data": data,
    }


def _records(envelope: Any) -> List[Any]:
    """Return the ``data`` records of an envelope (empty list if none)."""
    return _as_envelope_dict(envelope).get("data", [])


def _is_ok(envelope: Any) -> bool:
    return _as_envelope_dict(envelope).get("status") == STATUS_OK


def _is_error(envelope: Any) -> bool:
    return _as_envelope_dict(envelope).get("status") == STATUS_ERROR


@dataclass
class DrugAssessment:
    """The combined supply assessment for a single drug.

    Attributes:
        drug_name:       The drug the assessment is about.
        in_shortage:     Whether FDA shortage records were returned.
        shortage_count:  Number of shortage records.
        recall_count:    Number of recall records.
        on_hand:         Total on-hand quantity across the matched inventory.
        inventory_lines: The matched inventory stock entries.
        alternatives:    Therapeutic alternatives surfaced for this drug.
        risk_level:      Derived supply-risk level (see ``RISK_*``).
        recommendations: Short ordered list of recommended next actions.
        notes:           Any tool-degradation / error notes to surface.
    """

    drug_name: str
    in_shortage: bool = False
    shortage_count: int = 0
    recall_count: int = 0
    on_hand: float = 0.0
    inventory_lines: List[Dict[str, Any]] = field(default_factory=list)
    alternatives: List[Dict[str, Any]] = field(default_factory=list)
    risk_level: str = RISK_NONE
    recommendations: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "drug_name": self.drug_name,
            "in_shortage": self.in_shortage,
            "shortage_count": self.shortage_count,
            "recall_count": self.recall_count,
            "on_hand": self.on_hand,
            "inventory_lines": self.inventory_lines,
            "alternatives": self.alternatives,
            "risk_level": self.risk_level,
            "recommendations": self.recommendations,
            "notes": self.notes,
        }


def _match_inventory_for_drug(
    drug_name: str, stock_entries: List[Any]
) -> List[Dict[str, Any]]:
    """Return inventory stock entries whose drug name matches ``drug_name``.

    Matching is case-insensitive and substring-based so "amoxicillin" matches
    "Amoxicillin 500mg". Non-dict entries are ignored defensively.
    """
    needle = (drug_name or "").strip().lower()
    matches: List[Dict[str, Any]] = []
    if not needle:
        return matches
    for entry in stock_entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("drug_name", "")).lower()
        if needle and (needle in name or name in needle) and name:
            matches.append(entry)
    return matches


def _total_quantity(entries: List[Dict[str, Any]]) -> float:
    total = 0.0
    for entry in entries:
        try:
            total += float(entry.get("quantity", 0) or 0)
        except (TypeError, ValueError):
            continue
    return total


def _derive_risk(
    *, in_shortage: bool, recall_count: int, on_hand: float, has_low_stock: bool
) -> str:
    """Derive a supply-risk level from the composed signals.

    The rules are intentionally simple and deterministic:

    * A recall OR (shortage AND no stock on hand) -> ``HIGH``.
    * A shortage with some stock, or shortage plus a low-stock flag ->
      ``MODERATE``.
    * A low-stock flag alone -> ``LOW``.
    * Otherwise -> ``NONE``.
    """
    if recall_count > 0 or (in_shortage and on_hand <= 0):
        return RISK_HIGH
    if in_shortage:
        return RISK_MODERATE
    if has_low_stock:
        return RISK_LOW
    return RISK_NONE


def compose_drug_assessment(
    drug_name: str,
    *,
    shortages: Any = None,
    recalls: Any = None,
    stock_levels: Any = None,
    low_stock: Any = None,
    alternatives: Any = None,
) -> DrugAssessment:
    """Compose the combined assessment for one drug from tool envelopes.

    Each keyword argument is the structured envelope returned by the matching
    Gateway tool (or ``None`` if that tool was not called). The function joins
    the FDA supply signals to the pharmacy's on-hand inventory and derives a
    risk level plus recommended actions (Requirement 7.4).

    Args:
        drug_name:    Drug the assessment is about.
        shortages:    Envelope from ``search_drug_shortages``.
        recalls:      Envelope from ``get_recalls``.
        stock_levels: Envelope from ``get_stock_levels``.
        low_stock:    Envelope from ``get_low_stock_items``.
        alternatives: Envelope from ``find_therapeutic_alternatives``.

    Returns:
        A :class:`DrugAssessment`.
    """
    assessment = DrugAssessment(drug_name=drug_name)

    shortage_records = _records(shortages) if shortages is not None else []
    recall_records = _records(recalls) if recalls is not None else []
    assessment.shortage_count = len(shortage_records)
    assessment.recall_count = len(recall_records)
    assessment.in_shortage = _is_ok(shortages) and assessment.shortage_count > 0

    # Join inventory: match this drug within the location's stock levels.
    stock_entries = _records(stock_levels) if stock_levels is not None else []
    matched = _match_inventory_for_drug(drug_name, stock_entries)
    assessment.inventory_lines = matched
    assessment.on_hand = _total_quantity(matched)

    # Low-stock flag: is this drug among the location's low-stock items?
    low_stock_entries = _records(low_stock) if low_stock is not None else []
    has_low_stock = len(_match_inventory_for_drug(drug_name, low_stock_entries)) > 0

    # Alternatives (surfaced when supply is at risk).
    if alternatives is not None and _is_ok(alternatives):
        alt_records = _records(alternatives)
        assessment.alternatives = [a for a in alt_records if isinstance(a, dict)]

    # Carry through tool degradation so the narration can be honest.
    for label, env in (
        ("shortage lookup", shortages),
        ("recall lookup", recalls),
        ("stock lookup", stock_levels),
        ("low-stock lookup", low_stock),
        ("alternatives lookup", alternatives),
    ):
        if env is not None and _is_error(env):
            msg = _as_envelope_dict(env).get("message") or "tool error"
            assessment.notes.append(f"{label} unavailable: {msg}")

    assessment.risk_level = _derive_risk(
        in_shortage=assessment.in_shortage,
        recall_count=assessment.recall_count,
        on_hand=assessment.on_hand,
        has_low_stock=has_low_stock,
    )

    assessment.recommendations = _recommendations(assessment, has_low_stock)
    return assessment


def _recommendations(assessment: DrugAssessment, has_low_stock: bool) -> List[str]:
    """Produce an ordered list of recommended actions for an assessment."""
    recs: List[str] = []
    if assessment.recall_count > 0:
        recs.append(
            f"Quarantine affected lots of {assessment.drug_name}; a recall is active."
        )
    if assessment.in_shortage:
        if assessment.on_hand <= 0:
            recs.append(
                f"{assessment.drug_name} is in shortage and no stock is on hand; "
                "source a substitute urgently."
            )
        else:
            recs.append(
                f"{assessment.drug_name} is in shortage; conserve on-hand stock "
                f"({assessment.on_hand:g} units) and plan replenishment."
            )
        if assessment.alternatives:
            recs.append(
                f"Consider {len(assessment.alternatives)} therapeutic alternative(s) "
                "sharing the same ingredient or pharmacologic class."
            )
    if has_low_stock and not assessment.in_shortage:
        recs.append(
            f"{assessment.drug_name} is below its reorder threshold; "
            "draft a replenishment order."
        )
    if not recs:
        recs.append(
            f"No FDA supply risk detected for {assessment.drug_name}; "
            "inventory looks adequate."
        )
    return recs


def compose_combined_assessment(
    drug_names: List[str],
    *,
    shortages_by_drug: Optional[Dict[str, Any]] = None,
    recalls_by_drug: Optional[Dict[str, Any]] = None,
    alternatives_by_drug: Optional[Dict[str, Any]] = None,
    stock_levels: Any = None,
    low_stock: Any = None,
    location_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Compose a single combined assessment across one or more drugs.

    This is the top-level composition the agent uses to answer a
    "compare FDA shortage vs. our inventory" question (Requirement 7.4). The
    FDA envelopes are provided per drug; the inventory envelopes
    (``stock_levels`` / ``low_stock``) are provided once for the location and
    joined to each drug.

    Args:
        drug_names:           Drugs to assess.
        shortages_by_drug:    Map drug -> ``search_drug_shortages`` envelope.
        recalls_by_drug:      Map drug -> ``get_recalls`` envelope.
        alternatives_by_drug: Map drug -> ``find_therapeutic_alternatives`` envelope.
        stock_levels:         ``get_stock_levels`` envelope for the location.
        low_stock:            ``get_low_stock_items`` envelope for the location.
        location_id:          The pharmacy location the inventory is from.

    Returns:
        A dict ``{location_id, overall_risk, drugs: [DrugAssessment.to_dict()], summary}``.
    """
    shortages_by_drug = shortages_by_drug or {}
    recalls_by_drug = recalls_by_drug or {}
    alternatives_by_drug = alternatives_by_drug or {}

    assessments: List[DrugAssessment] = []
    for drug in drug_names:
        assessments.append(
            compose_drug_assessment(
                drug,
                shortages=shortages_by_drug.get(drug),
                recalls=recalls_by_drug.get(drug),
                stock_levels=stock_levels,
                low_stock=low_stock,
                alternatives=alternatives_by_drug.get(drug),
            )
        )

    overall = _overall_risk([a.risk_level for a in assessments])
    return {
        "location_id": location_id,
        "overall_risk": overall,
        "drugs": [a.to_dict() for a in assessments],
        "summary": _overall_summary(assessments, overall, location_id),
    }


_RISK_ORDER = {RISK_NONE: 0, RISK_LOW: 1, RISK_MODERATE: 2, RISK_HIGH: 3}


def _overall_risk(levels: List[str]) -> str:
    """Return the most severe risk level across all assessed drugs."""
    if not levels:
        return RISK_NONE
    return max(levels, key=lambda lvl: _RISK_ORDER.get(lvl, 0))


def _overall_summary(
    assessments: List[DrugAssessment], overall: str, location_id: Optional[str]
) -> str:
    """Produce a one-line human-readable summary of the combined assessment."""
    at_risk = [a.drug_name for a in assessments if a.risk_level in (RISK_MODERATE, RISK_HIGH)]
    loc = f" at {location_id}" if location_id else ""
    if not assessments:
        return "No drugs were assessed."
    if at_risk:
        return (
            f"Overall supply risk{loc} is {overall}. At-risk drug(s): "
            + ", ".join(at_risk)
            + "."
        )
    return f"Overall supply risk{loc} is {overall}. No drugs are currently at elevated risk."


__all__ = [
    "RISK_NONE",
    "RISK_LOW",
    "RISK_MODERATE",
    "RISK_HIGH",
    "DrugAssessment",
    "compose_drug_assessment",
    "compose_combined_assessment",
]
