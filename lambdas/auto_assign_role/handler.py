"""Cognito post-confirmation trigger: auto-assign a role group.

This Lambda implements the self-registration auto-assignment contract for
PharmaPulse (Requirements 12.9, 12.10):

    WHEN a user completes self-registration, THE PharmaPulse SHALL
    automatically assign the user a Buyer or Analyst role that grants the
    corresponding order-submission or read-only permissions immediately.

It is wired as the Cognito **post-confirmation** trigger. Cognito invokes
it the moment a self-registered user is confirmed, so the group membership
(and therefore the role claim carried in subsequent tokens) applies
immediately for Gateway Policy and the inventory server to evaluate.

Role selection
--------------
The group a new user is placed in is resolved, in priority order, from:

1. A ``custom:role`` sign-up attribute, if the user supplied one and it
   names a known group (``buyer`` or ``analyst``).
2. The ``DEFAULT_GROUP`` environment variable (defaults to ``analyst`` —
   the least-privileged, read-only role, so a misconfigured or silent
   sign-up never grants order-submission by default).

The ``buyer`` and ``analyst`` group names are injected via the
``BUYER_GROUP`` / ``ANALYST_GROUP`` environment variables so the stack is
the single source of truth for group names.

The handler is defensive: it never raises on an unexpected role value
(it falls back to the default group) because a Cognito trigger that raises
would block the user's confirmation entirely.
"""

from __future__ import annotations

import os
from typing import Any, Dict

import boto3

_cognito = boto3.client("cognito-idp")


def _resolve_group(requested_role: str, buyer_group: str, analyst_group: str, default_group: str) -> str:
    """Map a requested role string to a known group name.

    Falls back to ``default_group`` when the requested role is empty or not
    one of the known groups, so order-submission is never granted by accident.
    """
    normalized = (requested_role or "").strip().lower()
    known = {
        "buyer": buyer_group,
        analyst_group.lower(): analyst_group,
        buyer_group.lower(): buyer_group,
        "analyst": analyst_group,
    }
    return known.get(normalized, default_group)


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Cognito post-confirmation Lambda entry point.

    Adds the newly confirmed user to the resolved role group and returns the
    (unmodified) event, as required by the Cognito trigger contract.
    """
    buyer_group = os.environ.get("BUYER_GROUP", "buyer")
    analyst_group = os.environ.get("ANALYST_GROUP", "analyst")
    default_group = os.environ.get("DEFAULT_GROUP", analyst_group)

    # Only act for the sign-up confirmation source; other post-confirmation
    # sources (e.g. forgot-password) must pass through untouched.
    trigger_source = event.get("triggerSource", "")
    if trigger_source not in (
        "PostConfirmation_ConfirmSignUp",
        "PostConfirmation_ConfirmForgotPassword",
    ):
        return event

    user_pool_id = event.get("userPoolId")
    user_name = event.get("userName")
    if not user_pool_id or not user_name:
        return event

    attributes = (event.get("request") or {}).get("userAttributes") or {}
    requested_role = attributes.get("custom:role", "")

    group_name = _resolve_group(
        requested_role, buyer_group, analyst_group, default_group
    )

    # Idempotent: adding a user already in the group is a no-op success.
    _cognito.admin_add_user_to_group(
        UserPoolId=user_pool_id,
        Username=user_name,
        GroupName=group_name,
    )

    return event
