"""Tenant identity helpers shared by the hosted API and provider proxy.

The hosted service key identifies the Brevitas account. A SaaS account can proxy
requests for many end customers, so optimization state also needs an opaque,
caller-supplied customer identifier. Raw credentials and customer identifiers are
never used as registry keys or persisted; only a domain-separated SHA-256 digest is.
"""
from __future__ import annotations

import hashlib


CUSTOMER_ID_HEADER = "x-brevitas-customer-id"
MAX_CUSTOMER_ID_LENGTH = 128


def normalize_customer_id(value: str | None) -> str:
    """Validate an opaque end-customer id used only for tenant partitioning."""
    customer_id = (value or "").strip()
    if len(customer_id) > MAX_CUSTOMER_ID_LENGTH:
        raise ValueError(f"customer id exceeds {MAX_CUSTOMER_ID_LENGTH} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in customer_id):
        raise ValueError("customer id cannot contain control characters")
    return customer_id


def tenant_key(credential: str, customer_id: str = "") -> str:
    """Return a non-secret tenant key.

    With no end-customer id this intentionally equals the legacy SHA-256 key hash,
    preserving existing single-tenant quality streams. With a customer id, domain
    separation prevents ambiguous concatenations and partitions every shared service
    credential into independent end-customer tenants.
    """
    customer_id = normalize_customer_id(customer_id)
    material = credential or ""
    if customer_id:
        material = f"{material}\0brevitas-customer\0{customer_id}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def short_tenant_key(value: str) -> str:
    """Bounded registry identifier; input is already a non-secret SHA-256 digest."""
    return (value or "")[:16]
