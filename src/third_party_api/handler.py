"""Demo 3rd-party API — stands in for the customer's app during the demo.

It is NOT part of the production solution; it exists so you can exercise the
whole path (Quick -> Gateway -> interceptor -> here) without a real vendor.

Behaviour:
  * Requires an  Authorization: Bearer <token>  header (the interceptor injects it).
  * Returns 401 if missing.
  * Echoes back the token prefix and the request body, so you can confirm the
    interceptor swapped the Entra token for the downstream bearer.
"""
import json


def lambda_handler(event, context):
    headers = event.get("headers") or {}
    # Function URL lowercases header names.
    auth = headers.get("authorization") or headers.get("Authorization") or ""

    if not auth.startswith("Bearer "):
        return {
            "statusCode": 401,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": "Missing downstream Bearer token"}),
        }

    token = auth[len("Bearer "):]
    body_raw = event.get("body") or "{}"
    try:
        body = json.loads(body_raw)
    except (ValueError, TypeError):
        body = {"_raw": body_raw}

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(
            {
                "ok": True,
                "message": "Demo 3rd-party API reached with a downstream bearer.",
                "received_token_prefix": token[:12] + "…" if token else None,
                "echo_body": body,
            }
        ),
    }
