"""Shared configuration constants for PharmaPulse CDK stacks.

Centralizes region placement (Requirement 17.6/17.7), tagging
(Requirement 17.5), resource naming, and the configurable approval
threshold (Requirement 6.5) so every stack references a single source
of truth.
"""

from __future__ import annotations

# -- Regions (Requirement 17.6) ------------------------------------------
PRIMARY_REGION = "us-east-2"
EDGE_REGION = "us-east-1"

# -- Tagging (Requirement 17.5) ------------------------------------------
TAG_PROJECT = "Project"
TAG_PROJECT_VALUE = "PharmaPulse"
TAG_OWNER = "Owner"
TAG_OWNER_VALUE = "Instructor"

# -- Domain / hosting (Requirement 14) -----------------------------------
HOSTED_ZONE_NAME = "awsteach.com"
DEMO_DOMAIN = "demo.awsteach.com"

# -- DynamoDB table names (Requirement 4, 5, 6) --------------------------
TABLE_INVENTORY = "pharmapulse-inventory"
TABLE_DRAFT_ORDERS = "pharmapulse-draft-orders"
TABLE_ORDERS = "pharmapulse-orders"
GSI_BY_SKU = "by-sku"

# -- Orders business rules (Requirement 6.5) -----------------------------
DEFAULT_APPROVAL_THRESHOLD = 5000  # USD

# -- openFDA caching / backoff (Requirement 3) ---------------------------
OPENFDA_CACHE_TTL_SECONDS = 300
OPENFDA_MAX_ATTEMPTS = 3

# -- Observability (Requirement 16.3) ------------------------------------
BILLING_ALARM_THRESHOLD_USD = 50
