"""Synthetic inventory seed generator for the inventory-orders server.

Produces ~200 synthetic SKUs distributed across exactly 3 fictional
pharmacy locations (Requirement 4.1), using only synthetic/public drug
names and synthetic NDC identifiers. The generated data contains **no
Protected Health Information and no real customer data** (Requirements
4.5, 21.1, 21.2, 21.3):

- Drug names are common, publicly known generic ingredient names (no
  patient, prescriber, or customer information of any kind).
- NDCs are synthesized deterministically in the standard 5-4-2 segment
  shape; they are placeholders for the demo and are not tied to any real
  product record.
- Locations are obviously fictional ("Northside", "Riverside", "Hillcrest").

The generator is deterministic given a seed so tests and the CDK seeding
custom resource (task 11) produce a stable data set. ``build_seed_items``
returns plain dicts matching the inventory table attributes; ``seed_inventory``
writes them through an :class:`InventoryRepository`.
"""

from __future__ import annotations

import random
from typing import Dict, List

from servers.inventory_orders.data_access import InventoryRepository

# Exactly three obviously fictional pharmacy locations (Requirement 4.1).
LOCATIONS: List[Dict[str, str]] = [
    {"location_id": "LOC-NORTH", "name": "Northside Community Pharmacy"},
    {"location_id": "LOC-RIVER", "name": "Riverside Care Pharmacy"},
    {"location_id": "LOC-HILL", "name": "Hillcrest Family Pharmacy"},
]

# Target SKU count "approximately 200" (Requirement 4.1).
TARGET_SKU_COUNT = 200

# Public, well-known generic drug (active-ingredient) names only. These are
# publicly available common medication names — no PHI, no customer data,
# and not linked to any real product/NDC record.
_DRUG_NAMES: List[str] = [
    "Acetaminophen", "Ibuprofen", "Amoxicillin", "Azithromycin", "Lisinopril",
    "Metformin", "Atorvastatin", "Amlodipine", "Omeprazole", "Losartan",
    "Albuterol", "Gabapentin", "Hydrochlorothiazide", "Sertraline", "Levothyroxine",
    "Montelukast", "Pantoprazole", "Furosemide", "Prednisone", "Citalopram",
    "Metoprolol", "Simvastatin", "Clopidogrel", "Fluoxetine", "Tramadol",
    "Cetirizine", "Loratadine", "Naproxen", "Cephalexin", "Ciprofloxacin",
    "Doxycycline", "Fluconazole", "Insulin Glargine", "Warfarin", "Duloxetine",
    "Escitalopram", "Rosuvastatin", "Pravastatin", "Spironolactone", "Carvedilol",
]

# Dosage forms / strengths used to build distinct SKUs from each drug name.
_FORMS: List[str] = [
    "10 mg tablet", "20 mg tablet", "40 mg tablet", "100 mg capsule",
    "250 mg tablet", "500 mg tablet", "5 mg/mL solution", "250 mg/5 mL suspension",
]


def _synthetic_ndc(rng: random.Random) -> str:
    """Build a synthetic NDC in the standard 5-4-2 segment shape.

    These are placeholder identifiers for the demo and do not correspond to
    any real product record (Requirement 21 — synthetic data only).
    """
    labeler = rng.randint(10000, 99999)
    product = rng.randint(1000, 9999)
    package = rng.randint(10, 99)
    return f"{labeler:05d}-{product:04d}-{package:02d}"


def build_seed_items(
    *, count: int = TARGET_SKU_COUNT, seed: int = 1337
) -> List[Dict[str, object]]:
    """Build ``count`` synthetic inventory items across the 3 locations.

    The items are distributed as evenly as possible across the three
    fictional locations. Each item carries the inventory attributes defined
    in design.md: ``sku_id``, ``drug_name``, ``ndc``, ``quantity``,
    ``reorder_threshold``, ``unit_price``, and ``location_id``.

    Deterministic for a given ``seed`` so tests and seeding are stable.
    """
    rng = random.Random(seed)
    items: List[Dict[str, object]] = []

    for i in range(count):
        # Round-robin across the exactly-three locations for even spread.
        location = LOCATIONS[i % len(LOCATIONS)]

        drug_name = _DRUG_NAMES[i % len(_DRUG_NAMES)]
        form = _FORMS[rng.randrange(len(_FORMS))]
        full_name = f"{drug_name} {form}"

        reorder_threshold = rng.randint(10, 60)
        # Spread quantities so some SKUs land below their reorder threshold
        # (exercises low-stock detection in task 4.2) and some above.
        quantity = rng.randint(0, 200)
        # Prices in a realistic-but-synthetic range, two decimal places.
        unit_price = round(rng.uniform(1.5, 450.0), 2)

        items.append(
            {
                "sku_id": f"SKU-{i:05d}",
                "drug_name": full_name,
                "ndc": _synthetic_ndc(rng),
                "quantity": quantity,
                "reorder_threshold": reorder_threshold,
                "unit_price": unit_price,
                "location_id": location["location_id"],
            }
        )

    return items


def seed_inventory(
    repository: InventoryRepository,
    *,
    count: int = TARGET_SKU_COUNT,
    seed: int = 1337,
) -> int:
    """Generate and persist synthetic inventory via ``repository``.

    Returns the number of items written. Uses a batch writer for efficiency.
    """
    items = build_seed_items(count=count, seed=seed)
    repository.batch_put_inventory(items)
    return len(items)


__all__ = [
    "LOCATIONS",
    "TARGET_SKU_COUNT",
    "build_seed_items",
    "seed_inventory",
]
