"""Custom backend behind API Gateway — the AgentCore Gateway's target.

Request flow reaching here:
  Quick --Entra JWT--> Gateway --(interceptor swaps Authorization to the per-user
  vendor bearer)--> API Gateway --> THIS Lambda --> 3rd-party API.

So by the time a request lands here the Authorization header already carries the
DOWNSTREAM bearer that the interceptor minted from the user's email. This backend
does NOT mint tokens; it is the façade that calls the real vendor API with that
bearer and shapes the response for the agent.

Why a backend + API Gateway target instead of an OpenAPI target pointed straight
at the vendor:
  * It is the AWS sample's own shape (Gateway target = Lambda/REST you own), not an
    OpenAPI target pointed at a host you don't control.
  * You own the façade, so you can map tool args -> vendor request, normalise vendor
    responses/errors, add retries/timeouts, and keep the vendor's quirks off the
    Gateway contract.
  * The vendor URL stays server-side; only your API Gateway URL is the Gateway target.

Env:
  THIRD_PARTY_BASE_URL   base URL of the real vendor API.
  REQUEST_TIMEOUT        seconds (default 15).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

THIRD_PARTY_BASE_URL = os.environ["THIRD_PARTY_BASE_URL"].rstrip("/")
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "15"))


def _resp(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _bearer(event) -> str | None:
    headers = event.get("headers") or {}
    # API Gateway may lowercase header names.
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    return auth[len("Bearer "):] if auth.startswith("Bearer ") else None


def lambda_handler(event, context):
    """API Gateway (proxy) -> here. One route: POST /data -> vendor /data."""
    bearer = _bearer(event)
    if not bearer:
        # Interceptor should always have injected this; a miss means a wiring bug.
        return _resp(401, {"error": "Missing downstream bearer (interceptor?)"})

    # Parse the tool arguments the agent sent.
    raw = event.get("body") or "{}"
    try:
        args = json.loads(raw)
    except (ValueError, TypeError):
        args = {}

    # Call the real vendor API as the signed-in user, using the injected bearer.
    url = f"{THIRD_PARTY_BASE_URL}/data"
    payload = json.dumps(args).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {bearer}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:  # noqa: S310
            vendor_status = r.status
            vendor_body = r.read().decode()
    except urllib.error.HTTPError as e:
        return _resp(e.code, {"error": "Vendor API error", "detail": e.read().decode()[:500]})
    except urllib.error.URLError as e:
        return _resp(502, {"error": "Vendor API unreachable", "detail": str(e.reason)})

    try:
        parsed = json.loads(vendor_body)
    except (ValueError, TypeError):
        parsed = {"_raw": vendor_body}

    return _resp(vendor_status, {"ok": vendor_status < 400, "data": parsed})
