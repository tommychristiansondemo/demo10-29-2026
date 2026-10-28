"""Property-based test for therapeutic-alternatives membership (task 2.9).

# Feature: pharmapulse, Property 4: Alternatives share ingredient or class

Validates: Requirements 2.1, 2.2

Property 4 (design.md): *For any* drug input (name or NDC) and any mocked drug
catalog, every product returned by ``find_therapeutic_alternatives`` SHALL
share the input drug's active ingredient or its pharmacologic class.

The test drives :func:`find_therapeutic_alternatives` through a real
:class:`OpenFDAClient` wired to a **mock HTTP transport** that serves a
generated drug catalog. The transport routes responses by endpoint:

- ``drug/ndc.json``   -> the input drug's NDC-directory record (NDC path), and
- ``drug/label.json`` -> the full catalog of products. Both the input-profile
  resolution (by name) and the broad candidate query hit this endpoint, so the
  implementation sees every catalog product (input, true alternatives sharing
  the input's ingredient or class, and *noise* products that share neither).

The property assertion: every product returned shares the input's active
ingredient OR pharmacologic class, and no noise product is returned. The
catalog fields match the exact openFDA shapes the implementation extracts
(``openfda.generic_name`` / ``substance_name`` / ``pharm_class_*``,
``active_ingredients[].name``, and the top-level ``pharm_class`` /
``generic_name`` on the NDC record).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Set

from hypothesis import given, settings
from hypothesis import strategies as st

from shared.envelope import STATUS_EMPTY, STATUS_OK
from servers.fda_intel.openfda_client import (
    ENDPOINT_LABEL,
    ENDPOINT_NDC,
    HTTPResponse,
    OpenFDAClient,
    TTLCache,
)
from servers.fda_intel.tools import find_therapeutic_alternatives


# ---------------------------------------------------------------------------
# Catalog data model
# ---------------------------------------------------------------------------
# Small, closed vocabularies so generated products are likely to share (or not
# share) the input's ingredient/class. The property must hold regardless of
# overlap, but a constrained space makes meaningful cases (true alternatives +
# noise) frequent rather than vanishingly rare.
_INGREDIENTS = ["amoxicillin", "ibuprofen", "metformin", "lisinopril", "warfarin"]
_CLASSES = [
    "Penicillin-class Antibacterial",
    "Nonsteroidal Anti-inflammatory Drug",
    "Biguanide",
    "ACE Inhibitor",
    "Vitamin K Antagonist",
]


def _label_record(ingredients: List[str], classes: List[str]) -> Dict[str, Any]:
    """Build a ``drug/label.json``-shaped product record.

    Mirrors the fields the implementation extracts from label records:
    ``openfda.generic_name`` / ``openfda.substance_name`` (ingredients) and the
    split ``openfda.pharm_class_epc`` / ``_moa`` (classes).
    """
    return {
        "openfda": {
            "generic_name": list(ingredients),
            "substance_name": [i.upper() for i in ingredients],
            # Spread classes across two of the recognized pharm_class subfields.
            "pharm_class_epc": list(classes[:1]),
            "pharm_class_moa": list(classes[1:]),
        }
    }


def _ndc_record(ingredients: List[str], classes: List[str]) -> Dict[str, Any]:
    """Build a ``drug/ndc.json``-shaped product record for the input drug.

    Mirrors the NDC-directory shapes the implementation extracts: top-level
    ``generic_name``, structured ``active_ingredients[].name``, and the
    combined top-level ``pharm_class`` list.
    """
    return {
        "generic_name": " and ".join(ingredients) if ingredients else "",
        "active_ingredients": [{"name": i, "strength": "100 mg"} for i in ingredients],
        "pharm_class": list(classes),
    }


def _product_ingredients(ingredients: List[str]) -> Set[str]:
    return {i.strip().lower() for i in ingredients if i and i.strip()}


def _product_classes(classes: List[str]) -> Set[str]:
    return {c.strip().lower() for c in classes if c and c.strip()}


# ---------------------------------------------------------------------------
# Mock transport (routes by endpoint)
# ---------------------------------------------------------------------------
class CatalogTransport:
    """Mock transport serving a drug catalog, routed by openFDA endpoint.

    The alternatives flow issues two *kinds* of ``drug/label.json`` queries:

    1. A **profile-resolution** query that resolves the *input* drug by its
       name (``_label_name_query`` -> ``openfda.generic_name`` /
       ``openfda.brand_name`` for the input term). A faithful openFDA would
       return only the input drug here, so the mock returns just the input
       record for queries whose ``search`` references the input term.
    2. A broad **candidate** query matching any product that shares the input's
       ingredient/class, for which the mock returns the full catalog so the
       implementation's precise Python filter is exercised against real noise.

    Routing:
    - ``drug/ndc.json``   -> the input-drug NDC record (empty on the name path).
    - ``drug/label.json`` -> the input record alone when the ``search`` targets
      the input term, otherwise the full catalog.

    All responses are HTTP 200, so the retry path never triggers.
    """

    def __init__(
        self,
        *,
        input_label: Dict[str, Any],
        input_terms: Set[str],
        label_catalog: List[Dict[str, Any]],
        ndc_records: List[Dict[str, Any]],
    ) -> None:
        self._input_label = input_label
        # Lowercased terms that identify the *input* drug (its name and any
        # ingredient that doubles as the generic name used for resolution).
        self._input_terms = {t.strip().lower() for t in input_terms if t.strip()}
        self._label_catalog = label_catalog
        self._ndc_records = ndc_records
        self.calls = 0

    def _is_profile_query(self, search: str) -> bool:
        """True iff a label ``search`` targets the input drug's name fields.

        The profile-resolution query is the only label query built from
        ``openfda.brand_name``; the candidate query never references brand_name.
        """
        return "brand_name" in (search or "")

    def get(self, url: str, params: Dict[str, Any]) -> HTTPResponse:
        self.calls += 1
        if url.endswith(ENDPOINT_NDC):
            body = json.dumps({"results": self._ndc_records})
        elif url.endswith(ENDPOINT_LABEL):
            search = str(params.get("search", ""))
            if self._is_profile_query(search):
                # Resolution-by-name: a real openFDA returns only the input.
                body = json.dumps({"results": [self._input_label]})
            else:
                # Candidate query: serve the whole catalog (alternatives + noise).
                body = json.dumps({"results": self._label_catalog})
        else:  # pragma: no cover - alternatives only touches ndc/label
            body = json.dumps({"results": []})
        return HTTPResponse(status_code=200, body=body)


def _make_client(transport: CatalogTransport) -> OpenFDAClient:
    """Client wired to the mock transport with a no-op sleeper and fresh cache.

    A fresh pinned-clock cache per client prevents any cached value from a prior
    example leaking in and bypassing the transport under test.
    """
    return OpenFDAClient(
        transport=transport,
        sleeper=lambda _seconds: None,
        cache=TTLCache(ttl_seconds=300.0, clock=lambda: 0.0),
    )


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
# An input drug always has at least one ingredient and one class so there is a
# concrete profile to match on.
_input_ingredients = st.lists(
    st.sampled_from(_INGREDIENTS), min_size=1, max_size=2, unique=True
)
_input_classes = st.lists(
    st.sampled_from(_CLASSES), min_size=1, max_size=2, unique=True
)

# Catalog products may have any (possibly empty) set of ingredients/classes;
# this produces a mix of true alternatives (overlap) and noise (no overlap).
_product = st.fixed_dictionaries(
    {
        "ingredients": st.lists(
            st.sampled_from(_INGREDIENTS), min_size=0, max_size=2, unique=True
        ),
        "classes": st.lists(
            st.sampled_from(_CLASSES), min_size=0, max_size=2, unique=True
        ),
    }
)
_catalog = st.lists(_product, min_size=0, max_size=8)


@settings(max_examples=200)
@given(
    input_ingredients=_input_ingredients,
    input_classes=_input_classes,
    catalog=_catalog,
    use_ndc=st.booleans(),
)
def test_alternatives_share_ingredient_or_class(
    input_ingredients: List[str],
    input_classes: List[str],
    catalog: List[Dict[str, List[str]]],
    use_ndc: bool,
) -> None:
    # Feature: pharmapulse, Property 4: Alternatives share ingredient or class
    # Validates: Requirements 2.1, 2.2
    input_ing_set = _product_ingredients(input_ingredients)
    input_cls_set = _product_classes(input_classes)

    # The input drug's label record (resolution-by-name returns this alone),
    # plus the generated catalog of candidate/noise products served to the
    # broad candidate query.
    input_label = _label_record(input_ingredients, input_classes)
    label_catalog: List[Dict[str, Any]] = [input_label]
    for product in catalog:
        label_catalog.append(_label_record(product["ingredients"], product["classes"]))

    # NDC path: serve the input drug's NDC record so profile resolution works
    # from the NDC directory; name path: serve no NDC records.
    ndc_records = [_ndc_record(input_ingredients, input_classes)] if use_ndc else []

    transport = CatalogTransport(
        input_label=input_label,
        input_terms=set(input_ingredients),
        label_catalog=label_catalog,
        ndc_records=ndc_records,
    )
    client = _make_client(transport)

    if use_ndc:
        envelope = find_therapeutic_alternatives(client, ndc="12345-678-90")
    else:
        # Resolve by the input's generic name (first ingredient is a valid one).
        envelope = find_therapeutic_alternatives(client, drug_name=input_ingredients[0])

    # Result is a well-formed envelope: either OK with products or EMPTY.
    assert envelope["status"] in (STATUS_OK, STATUS_EMPTY)

    for record in envelope["data"]:
        openfda = record.get("openfda", {})
        record_ings = _product_ingredients(openfda.get("generic_name", []))
        record_clss = _product_classes(
            list(openfda.get("pharm_class_epc", []))
            + list(openfda.get("pharm_class_moa", []))
        )

        shares_ingredient = bool(record_ings & input_ing_set)
        shares_class = bool(record_clss & input_cls_set)

        # Property 4: every returned product shares the input's active
        # ingredient OR its pharmacologic class.
        assert shares_ingredient or shares_class, (
            "find_therapeutic_alternatives returned a product that shares "
            f"neither the input ingredient {input_ing_set} nor class "
            f"{input_cls_set}: ingredients={record_ings}, classes={record_clss}"
        )


# A noise product draws its ingredients/classes only from the "other" halves of
# the vocabularies, so it provably shares neither with a single fixed input.
# We fix the input to the first vocabulary entries so the disjoint halves are
# well-defined and the remaining entries are guaranteed non-overlapping.
_FIXED_INPUT_INGREDIENT = _INGREDIENTS[0]
_FIXED_INPUT_CLASS = _CLASSES[0]
_OTHER_INGREDIENTS = _INGREDIENTS[1:]
_OTHER_CLASSES = _CLASSES[1:]

_noise_product = st.fixed_dictionaries(
    {
        "ingredients": st.lists(
            st.sampled_from(_OTHER_INGREDIENTS), min_size=0, max_size=2, unique=True
        ),
        "classes": st.lists(
            st.sampled_from(_OTHER_CLASSES), min_size=0, max_size=2, unique=True
        ),
    }
)


@settings(max_examples=100)
@given(noise_catalog=st.lists(_noise_product, min_size=1, max_size=8))
def test_noise_products_are_excluded(
    noise_catalog: List[Dict[str, List[str]]],
) -> None:
    # Feature: pharmapulse, Property 4: Alternatives share ingredient or class
    # Validates: Requirements 2.1, 2.2
    #
    # Construct a catalog where every non-input product shares neither the
    # input's ingredient nor its class (pure noise, drawn from disjoint
    # vocabularies). The only product that may legitimately be returned is the
    # input itself; no noise product may be returned.
    input_ingredients = [_FIXED_INPUT_INGREDIENT]
    input_classes = [_FIXED_INPUT_CLASS]
    input_ing_set = _product_ingredients(input_ingredients)
    input_cls_set = _product_classes(input_classes)

    input_label = _label_record(input_ingredients, input_classes)
    label_catalog: List[Dict[str, Any]] = [input_label]
    for product in noise_catalog:
        label_catalog.append(
            _label_record(product["ingredients"], product["classes"])
        )

    transport = CatalogTransport(
        input_label=input_label,
        input_terms=set(input_ingredients),
        label_catalog=label_catalog,
        ndc_records=[],
    )
    client = _make_client(transport)

    envelope = find_therapeutic_alternatives(client, drug_name=input_ingredients[0])

    for record in envelope["data"]:
        openfda = record.get("openfda", {})
        record_ings = _product_ingredients(openfda.get("generic_name", []))
        record_clss = _product_classes(
            list(openfda.get("pharm_class_epc", []))
            + list(openfda.get("pharm_class_moa", []))
        )
        # Any returned product must genuinely share ingredient/class; no pure
        # noise product should slip through the precise in-Python filter.
        assert (record_ings & input_ing_set) or (record_clss & input_cls_set), (
            "A noise product (sharing neither ingredient nor class) was "
            f"returned: ingredients={record_ings}, classes={record_clss}"
        )
