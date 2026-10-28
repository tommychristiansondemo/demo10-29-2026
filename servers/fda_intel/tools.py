"""FDA lookup tool logic for the fda-intel MCP server.

This module implements the pure/mockable tool logic for the three FDA
lookup operations (task 2.4):

- :func:`search_drug_shortages` — ``drug/drugshortages.json``   (Req 1.1)
- :func:`get_recalls`           — ``drug/enforcement.json``     (Req 1.2)
- :func:`lookup_ndc`            — ``drug/ndc.json``             (Req 1.3)

Each tool builds an endpoint-specific openFDA query, delegates the HTTP +
cache + backoff concerns to :class:`~servers.fda_intel.openfda_client.OpenFDAClient`,
and maps the outcome onto the shared structured envelope
(:mod:`shared.envelope`):

- Upstream records present      -> ``status=OK``   with ``data`` = records   (1.1/1.2/1.3)
- Upstream returned zero records -> ``status=EMPTY`` with a "no records found"
  message and empty ``data``                                                 (1.4)
- Upstream raised an error       -> ``status=ERROR`` whose message identifies
  the failed operation                                                       (1.5)

The tools are deliberately independent of FastMCP so they can be unit- and
property-tested cheaply with a mock transport. The FastMCP registration /
server entrypoint is task 7.1 and lives elsewhere.

The therapeutic-alternatives tool (``find_therapeutic_alternatives``) is task
2.8 and is intentionally NOT implemented here; this module is structured so it
can be added alongside these lookups later.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

from shared.envelope import empty, error, ok

from servers.fda_intel.openfda_client import (
    OpenFDAClient,
    OpenFDAError,
    OpenFDAUnavailableError,
)

# openFDA caps the drugshortages endpoint ``limit`` at 100 records; we clamp
# any larger request down to this ceiling (design: "limit clamping (<=100
# shortages)"). A sensible floor of 1 guards against non-positive input.
SHORTAGES_MAX_LIMIT = 100
DEFAULT_LIMIT = 10
MIN_LIMIT = 1

# Logical operation names used in ERROR messages so the agent/tests can tell
# which lookup failed (Requirement 1.5).
OP_SHORTAGES = "search_drug_shortages"
OP_RECALLS = "get_recalls"
OP_NDC = "lookup_ndc"
OP_ALTERNATIVES = "find_therapeutic_alternatives"

# How many label/ndc records to pull when discovering candidate alternatives.
ALTERNATIVES_FETCH_LIMIT = 50


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _clamp_limit(limit: Optional[int], *, maximum: int) -> int:
    """Clamp a requested ``limit`` into ``[MIN_LIMIT, maximum]``.

    ``None`` or non-integer input falls back to :data:`DEFAULT_LIMIT`. Values
    below ``MIN_LIMIT`` are raised to the floor and values above ``maximum``
    are lowered to the ceiling, which enforces the openFDA shortages cap of
    100 while keeping requests well-formed.
    """
    if limit is None:
        return DEFAULT_LIMIT
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    if value < MIN_LIMIT:
        return MIN_LIMIT
    if value > maximum:
        return maximum
    return value


def _quote(term: str) -> str:
    """Wrap a free-text term in quotes for an openFDA ``search`` phrase.

    Quoting keeps multi-word drug names (e.g. ``sodium chloride``) together as
    a single phrase match rather than an implicit OR across tokens.
    """
    cleaned = (term or "").strip().replace('"', "")
    return f'"{cleaned}"'


def _shortages_query(drug_name: str) -> str:
    """Build the ``search`` expression for the drugshortages endpoint.

    The shortages dataset identifies the product via ``generic_name`` and
    ``proprietary_name``; matching either lets buyers search by the common or
    brand name.
    """
    term = _quote(drug_name)
    return f"generic_name:{term}+proprietary_name:{term}"


def _recalls_query(drug_name: str) -> str:
    """Build the ``search`` expression for the enforcement (recalls) endpoint.

    Enforcement records describe the product in ``openfda.generic_name`` /
    ``openfda.brand_name`` and in the free-text ``product_description``;
    searching all three broadens recall coverage for a drug name.
    """
    term = _quote(drug_name)
    return (
        f"openfda.generic_name:{term}"
        f"+openfda.brand_name:{term}"
        f"+product_description:{term}"
    )


def normalize_ndc(ndc: str) -> str:
    """Normalize NDC input for the ``drug/ndc.json`` lookup.

    openFDA stores NDCs in hyphenated segment form, but users commonly paste
    codes with surrounding whitespace or inconsistent spacing. We trim and
    collapse internal whitespace and drop stray characters that are not part
    of an NDC (digits, hyphens, and ``*`` placeholders), preserving the
    hyphenated segment structure the directory indexes on.
    """
    if not ndc:
        return ""
    collapsed = "".join((ndc or "").split())
    allowed = set("0123456789-*")
    return "".join(ch for ch in collapsed if ch in allowed)


def _ndc_query(ndc: str) -> str:
    """Build the ``search`` expression for the NDC directory endpoint.

    The normalized code is matched against both ``product_ndc`` and
    ``package_ndc`` so either a product-level or package-level NDC resolves.
    """
    term = _quote(ndc)
    return f"product_ndc:{term}+package_ndc:{term}"


def _no_records_message(subject: str) -> str:
    """Build the EMPTY-result message (Requirement 1.4)."""
    return f"No records found for {subject}."


def _error_message(operation: str, exc: OpenFDAError) -> str:
    """Build an ERROR message identifying the failed operation (Req 1.5)."""
    return f"{operation} failed: {exc}"


# ---------------------------------------------------------------------------
# Lookup tools
# ---------------------------------------------------------------------------
def search_drug_shortages(
    client: OpenFDAClient, drug_name: str, limit: Optional[int] = DEFAULT_LIMIT
) -> Dict[str, Any]:
    """Look up FDA drug shortage records for a drug name.

    Queries ``drug/drugshortages.json`` for shortage records matching the
    drug's generic or proprietary name, clamping ``limit`` to the openFDA
    ceiling of 100 (Requirements 1.1, 1.4, 1.5).

    Returns a :mod:`shared.envelope` dict: ``OK`` with the records, ``EMPTY``
    with a "no records found" message when none match, or ``ERROR`` naming the
    operation when openFDA fails.
    """
    clamped = _clamp_limit(limit, maximum=SHORTAGES_MAX_LIMIT)
    try:
        records = client.search_shortages(_shortages_query(drug_name), limit=clamped)
    except (OpenFDAError, OpenFDAUnavailableError) as exc:
        return error(_error_message(OP_SHORTAGES, exc))

    if not records:
        return empty(_no_records_message(f"drug shortages matching '{drug_name}'"))
    return ok(records, message=f"Found {len(records)} shortage record(s) for '{drug_name}'.")


def get_recalls(
    client: OpenFDAClient, drug_name: str, limit: Optional[int] = DEFAULT_LIMIT
) -> Dict[str, Any]:
    """Look up FDA recall (enforcement) records for a drug name.

    Queries ``drug/enforcement.json`` for enforcement reports matching the
    drug's generic/brand name or product description (Requirements 1.2, 1.4,
    1.5). ``limit`` is bounded to the shortages ceiling for consistency across
    the lookup tools.

    Returns a :mod:`shared.envelope` dict (``OK`` / ``EMPTY`` / ``ERROR``).
    """
    clamped = _clamp_limit(limit, maximum=SHORTAGES_MAX_LIMIT)
    try:
        records = client.search_enforcement(_recalls_query(drug_name), limit=clamped)
    except (OpenFDAError, OpenFDAUnavailableError) as exc:
        return error(_error_message(OP_RECALLS, exc))

    if not records:
        return empty(_no_records_message(f"recalls matching '{drug_name}'"))
    return ok(records, message=f"Found {len(records)} recall record(s) for '{drug_name}'.")


def lookup_ndc(client: OpenFDAClient, ndc: str) -> Dict[str, Any]:
    """Look up NDC product details for a National Drug Code.

    Normalizes the NDC input, then queries ``drug/ndc.json`` for the product
    and package details (Requirements 1.3, 1.4, 1.5).

    Returns a :mod:`shared.envelope` dict (``OK`` / ``EMPTY`` / ``ERROR``).
    """
    normalized = normalize_ndc(ndc)
    try:
        records = client.lookup_ndc(_ndc_query(normalized), limit=DEFAULT_LIMIT)
    except (OpenFDAError, OpenFDAUnavailableError) as exc:
        return error(_error_message(OP_NDC, exc))

    if not records:
        return empty(_no_records_message(f"NDC '{normalized or ndc}'"))
    return ok(records, message=f"Found {len(records)} product record(s) for NDC '{normalized}'.")


# ---------------------------------------------------------------------------
# Therapeutic alternatives (task 2.8)
# ---------------------------------------------------------------------------
# openFDA exposes a drug's active ingredient(s) and pharmacologic class across
# both ``drug/label.json`` and ``drug/ndc.json``. The two datasets use slightly
# different field shapes, so we read from several known locations and normalize
# everything to lowercase sets for robust, case-insensitive matching.
#
# Active-ingredient fields:
#   - label: ``openfda.generic_name`` (list), ``openfda.substance_name`` (list)
#   - ndc:   ``generic_name`` (str), ``active_ingredients[].name`` (list of obj)
#
# Pharmacologic-class fields (openFDA splits class by type):
#   - label/ndc ``openfda.pharm_class_epc`` / ``_moa`` / ``_cs`` / ``_pe`` (lists)
#   - ndc ``pharm_class`` (list of str)
_INGREDIENT_LIST_FIELDS = ("generic_name", "substance_name")
_PHARM_CLASS_FIELDS = (
    "pharm_class_epc",
    "pharm_class_moa",
    "pharm_class_cs",
    "pharm_class_pe",
)


def _as_str_list(value: Any) -> List[str]:
    """Coerce a scalar/list value into a flat list of non-empty strings."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        out: List[str] = []
        for item in value:
            out.extend(_as_str_list(item))
        return out
    text = str(value).strip()
    return [text] if text else []


def _normset(values: Any) -> Set[str]:
    """Normalize values into a lowercase, stripped set for matching."""
    return {v.strip().lower() for v in _as_str_list(values) if v.strip()}


def _openfda_block(record: Dict[str, Any]) -> Dict[str, Any]:
    """Return a record's ``openfda`` sub-block (or an empty dict)."""
    block = record.get("openfda")
    return block if isinstance(block, dict) else {}


def _extract_ingredients(record: Dict[str, Any]) -> Set[str]:
    """Collect a record's active-ingredient names from label/ndc shapes."""
    if not isinstance(record, dict):
        return set()
    ingredients: Set[str] = set()
    openfda = _openfda_block(record)

    # label + ndc: generic/substance names can live at top level or in openfda.
    for field_name in _INGREDIENT_LIST_FIELDS:
        ingredients |= _normset(record.get(field_name))
        ingredients |= _normset(openfda.get(field_name))

    # ndc: structured active_ingredients list of {"name": ..., "strength": ...}.
    active = record.get("active_ingredients")
    if isinstance(active, (list, tuple)):
        for entry in active:
            if isinstance(entry, dict):
                ingredients |= _normset(entry.get("name"))
            else:
                ingredients |= _normset(entry)
    return ingredients


def _extract_classes(record: Dict[str, Any]) -> Set[str]:
    """Collect a record's pharmacologic class values from label/ndc shapes."""
    if not isinstance(record, dict):
        return set()
    classes: Set[str] = set()
    openfda = _openfda_block(record)

    for field_name in _PHARM_CLASS_FIELDS:
        classes |= _normset(record.get(field_name))
        classes |= _normset(openfda.get(field_name))

    # ndc directory exposes a combined ``pharm_class`` list of strings.
    classes |= _normset(record.get("pharm_class"))
    classes |= _normset(openfda.get("pharm_class"))
    return classes


def _resolve_profile(
    client: OpenFDAClient, *, drug_name: Optional[str], ndc: Optional[str]
) -> Tuple[Set[str], Set[str]]:
    """Resolve the input drug's active ingredients and pharmacologic classes.

    When an NDC is supplied, the ``drug/ndc.json`` directory is consulted first
    (it is the authoritative identifier), then ``drug/label.json`` is used to
    enrich the class/ingredient profile. When only a name is supplied, the label
    endpoint is used. The union across whatever resolved is returned so a match
    on either dataset's ingredient or class is possible.
    """
    ingredients: Set[str] = set()
    classes: Set[str] = set()

    # NDC path: resolve via the NDC directory, and fall back to its generic
    # name for the label enrichment query below.
    resolved_name = (drug_name or "").strip()
    if ndc:
        normalized = normalize_ndc(ndc)
        ndc_records = client.lookup_ndc(_ndc_query(normalized), limit=DEFAULT_LIMIT)
        for record in ndc_records:
            ingredients |= _extract_ingredients(record)
            classes |= _extract_classes(record)
            if not resolved_name:
                names = _as_str_list(record.get("generic_name")) or _as_str_list(
                    _openfda_block(record).get("generic_name")
                )
                if names:
                    resolved_name = names[0]

    # Label path: query by the (possibly NDC-resolved) generic name.
    if resolved_name:
        label_records = client.search_label(
            _label_name_query(resolved_name), limit=DEFAULT_LIMIT
        )
        for record in label_records:
            ingredients |= _extract_ingredients(record)
            classes |= _extract_classes(record)

    return ingredients, classes


def _label_name_query(drug_name: str) -> str:
    """Build the ``search`` expression resolving a drug by name on label.json.

    ``drug/label.json`` describes the product through ``openfda.generic_name``
    and ``openfda.brand_name``; matching either resolves common or brand names.
    """
    term = _quote(drug_name)
    return f"openfda.generic_name:{term}+openfda.brand_name:{term}"


def _alternatives_candidate_query(ingredients: Set[str], classes: Set[str]) -> str:
    """Build a label.json query matching the input's ingredient OR class.

    Each resolved ingredient/class contributes an OR clause (openFDA uses ``+``
    as the OR separator within a ``search`` expression), so the returned
    candidate set is a superset we then filter precisely in Python to guarantee
    Property 4.
    """
    clauses: List[str] = []
    for ingredient in sorted(ingredients):
        clauses.append(f"openfda.generic_name:{_quote(ingredient)}")
        clauses.append(f"openfda.substance_name:{_quote(ingredient)}")
    for cls in sorted(classes):
        for field_name in _PHARM_CLASS_FIELDS:
            clauses.append(f"openfda.{field_name}:{_quote(cls)}")
    return "+".join(clauses)


def _matches_input(
    record: Dict[str, Any], ingredients: Set[str], classes: Set[str]
) -> bool:
    """True iff ``record`` shares an active ingredient or class with the input.

    This precise, in-Python check is what guarantees Property 4: every product
    we return must share the input's active ingredient OR its pharmacologic
    class, regardless of what the broad upstream candidate query returned.
    """
    if ingredients and (_extract_ingredients(record) & ingredients):
        return True
    if classes and (_extract_classes(record) & classes):
        return True
    return False


def find_therapeutic_alternatives(
    client: OpenFDAClient,
    drug_name: Optional[str] = None,
    ndc: Optional[str] = None,
) -> Dict[str, Any]:
    """Find therapeutic alternatives for a drug identified by name or NDC.

    Resolves the input drug's active ingredient(s) and pharmacologic class via
    ``drug/ndc.json`` (when an NDC is given) and ``drug/label.json``, then
    queries ``drug/label.json`` for other products sharing the same active
    ingredient OR the same pharmacologic class (Requirements 2.1, 2.2).

    Every returned product is filtered to share the input's ingredient or class,
    so the result set honors Property 4. Returns a :mod:`shared.envelope` dict:

    - ``OK`` with the alternative products that share ingredient/class,
    - ``EMPTY`` with a "no alternatives found" message when none match (2.3),
    - ``ERROR`` naming the operation when openFDA fails (1.5).
    """
    subject = (drug_name or "").strip() or normalize_ndc(ndc or "") or "the requested drug"

    if not (drug_name and drug_name.strip()) and not (ndc and ndc.strip()):
        return error(
            f"{OP_ALTERNATIVES} failed: a drug_name or ndc is required."
        )

    try:
        ingredients, classes = _resolve_profile(client, drug_name=drug_name, ndc=ndc)

        # Could not determine what to match on -> no alternatives to offer.
        if not ingredients and not classes:
            return empty(_no_alternatives_message(subject))

        candidates = client.search_label(
            _alternatives_candidate_query(ingredients, classes),
            limit=ALTERNATIVES_FETCH_LIMIT,
        )
    except (OpenFDAError, OpenFDAUnavailableError) as exc:
        return error(_error_message(OP_ALTERNATIVES, exc))

    # Precise, in-Python filter: keep only products that genuinely share the
    # input's ingredient or class (Property 4).
    alternatives = [
        record
        for record in candidates
        if isinstance(record, dict) and _matches_input(record, ingredients, classes)
    ]

    if not alternatives:
        return empty(_no_alternatives_message(subject))
    return ok(
        alternatives,
        message=f"Found {len(alternatives)} therapeutic alternative(s) for {subject}.",
    )


def _no_alternatives_message(subject: str) -> str:
    """Build the EMPTY-result message for alternatives (Requirement 2.3)."""
    return f"No therapeutic alternatives found for {subject}."


__all__ = [
    "SHORTAGES_MAX_LIMIT",
    "DEFAULT_LIMIT",
    "search_drug_shortages",
    "get_recalls",
    "lookup_ndc",
    "find_therapeutic_alternatives",
    "normalize_ndc",
]
