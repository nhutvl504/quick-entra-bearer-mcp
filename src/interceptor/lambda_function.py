"""
Request Lambda Interceptor — Entra ID (inbound JWT) -> email -> special endpoint
-> per-user bearer token -> 3rd-party API (outbound).

Based on the AWS sample "Custom Authentication with Request Lambda Interceptor".
The ORIGINAL sample does: validate Cognito JWT -> read a STATIC service-account
credential from Secrets Manager -> build a Basic Auth header.

THIS skeleton changes the OUTBOUND half to your flow:
  1. Validate the inbound Entra ID (Azure AD) JWT  -- defense in depth.
  2. Read the user's EMAIL claim from the validated token.
  3. Call your "special endpoint" with that email to mint a PER-USER bearer token.
     (cached per-email with TTL, so you don't mint on every single tool call.)
  4. Inject  Authorization: Bearer <token>  and forward to the 3rd-party API.

The agent never sees the bearer token: it is minted and injected here, on the
Gateway -> target hop, exactly like the sample keeps the API key off the agent.

ENV VARS expected:
  ENTRA_TENANT_ID        Azure AD tenant id (GUID)  -> issuer + JWKS
  ENTRA_AUDIENCE         expected 'aud' of the inbound token (your API/app id URI)
  EMAIL_CLAIM            claim holding the email (default: "email"; often "preferred_username"
                         or "upn" in Entra ID -- CHECK YOUR TOKEN)
  TOKEN_ENDPOINT         the "special endpoint" that returns a bearer given an email
  TOKEN_ENDPOINT_SECRET_NAME   Secrets Manager secret holding any credential the
                               special endpoint itself needs (api key / client secret)
  TOKEN_CACHE_TTL        seconds to cache a minted bearer per email (default 300)
  JWKS_CACHE_TTL         seconds to cache Entra JWKS (default 3600)
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.request

import boto3
import jwt
from jwt import PyJWKClient

logger = logging.getLogger()
logger.setLevel(logging.INFO)

secrets_client = boto3.client("secretsmanager")

# ── Config from environment ──────────────────────────────────────────────
ENTRA_TENANT_ID = os.environ["ENTRA_TENANT_ID"]
ENTRA_AUDIENCE = os.environ["ENTRA_AUDIENCE"]
EMAIL_CLAIM = os.environ.get("EMAIL_CLAIM", "email")
TOKEN_ENDPOINT = os.environ["TOKEN_ENDPOINT"]
TOKEN_ENDPOINT_SECRET_NAME = os.environ.get("TOKEN_ENDPOINT_SECRET_NAME", "")
TOKEN_CACHE_TTL = int(os.environ.get("TOKEN_CACHE_TTL", "300"))
JWKS_CACHE_TTL = int(os.environ.get("JWKS_CACHE_TTL", "3600"))

# Entra ID v2.0 issuer + JWKS. (If your tokens are v1.0, issuer is
# https://sts.windows.net/{tenant}/ and the metadata URL differs -- verify.)
ENTRA_ISSUER = f"https://login.microsoftonline.com/{ENTRA_TENANT_ID}/v2.0"
JWKS_URL = f"https://login.microsoftonline.com/{ENTRA_TENANT_ID}/discovery/v2.0/keys"

# ── Module-level caches (survive warm invocations) ───────────────────────
_JWK_CLIENT = None
_ENDPOINT_SECRET = None
_ENDPOINT_SECRET_AT = 0.0
# email -> {"token": str, "exp": float}
_TOKEN_CACHE: dict[str, dict] = {}


# ─────────────────────────────────────────────────────────────────────────
# Inbound: validate the Entra ID JWT and pull the email claim
# ─────────────────────────────────────────────────────────────────────────
def _jwk_client() -> PyJWKClient:
    global _JWK_CLIENT
    if _JWK_CLIENT is None:
        # PyJWKClient caches keys in-process; lifecycle_cache keeps it fresh.
        _JWK_CLIENT = PyJWKClient(JWKS_URL, cache_keys=True, lifespan=JWKS_CACHE_TTL)
    return _JWK_CLIENT


def validate_entra_jwt(token: str) -> dict | None:
    """Validate signature, expiry, issuer, audience. Return claims or None."""
    try:
        signing_key = _jwk_client().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=ENTRA_AUDIENCE,
            issuer=ENTRA_ISSUER,
        )
        logger.info("[JWT] Entra token valid (sub=%s).", claims.get("sub"))
        return claims
    except jwt.ExpiredSignatureError:
        logger.error("[JWT] Entra token expired.")
    except jwt.InvalidTokenError as e:
        logger.error("[JWT] Entra token invalid: %s", e)
    return None


def email_from_claims(claims: dict) -> str | None:
    """Entra puts email in different places depending on token/app config."""
    for key in (EMAIL_CLAIM, "email", "preferred_username", "upn"):
        val = claims.get(key)
        if val:
            return val
    return None


# ─────────────────────────────────────────────────────────────────────────
# Outbound: email -> special endpoint -> per-user bearer token (cached)
# ─────────────────────────────────────────────────────────────────────────
def _endpoint_secret() -> dict:
    """Credential the special endpoint itself requires (optional)."""
    global _ENDPOINT_SECRET, _ENDPOINT_SECRET_AT
    if not TOKEN_ENDPOINT_SECRET_NAME:
        return {}
    if _ENDPOINT_SECRET is None or (time.time() - _ENDPOINT_SECRET_AT) > TOKEN_CACHE_TTL:
        resp = secrets_client.get_secret_value(SecretId=TOKEN_ENDPOINT_SECRET_NAME)
        _ENDPOINT_SECRET = json.loads(resp["SecretString"])
        _ENDPOINT_SECRET_AT = time.time()
        logger.info("[Secret] Loaded special-endpoint credential.")
    return _ENDPOINT_SECRET


def mint_bearer_for_email(email: str) -> str | None:
    """Call the special endpoint to exchange email -> bearer. Cached per email."""
    now = time.time()
    cached = _TOKEN_CACHE.get(email)
    if cached and cached["exp"] > now:
        logger.info("[Token] Cache hit for %s.", email)
        return cached["token"]

    secret = _endpoint_secret()
    # ── ADAPT THIS to your special endpoint's real contract ──────────────
    payload = json.dumps({"email": email}).encode()
    req = urllib.request.Request(
        TOKEN_ENDPOINT,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            # e.g. the endpoint wants its own api key / client secret:
            **({"x-api-key": secret["api_key"]} if "api_key" in secret else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - trusted endpoint
            data = json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        logger.error("[Token] Special endpoint call failed: %s", e)
        return None

    # ── ADAPT: field names from the special endpoint's response ──────────
    token = data.get("access_token") or data.get("bearer") or data.get("token")
    if not token:
        logger.error("[Token] No token field in special-endpoint response.")
        return None

    # Honour the endpoint's own expiry if it returns one; else fall back to TTL.
    expires_in = int(data.get("expires_in", TOKEN_CACHE_TTL))
    # refresh 30s early to avoid using a token that dies mid-flight
    _TOKEN_CACHE[email] = {"token": token, "exp": now + max(expires_in - 30, 30)}
    logger.info("[Token] Minted + cached bearer for %s (ttl=%ss).", email, expires_in)
    return token


# ─────────────────────────────────────────────────────────────────────────
# Interceptor output helpers (exact shape the Gateway expects)
# ─────────────────────────────────────────────────────────────────────────
def _transformed_request(headers: dict, body: dict) -> dict:
    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {"transformedGatewayRequest": {"headers": headers, "body": body}},
    }


def _passthrough(gateway_request: dict) -> dict:
    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {
            "transformedGatewayRequest": {
                "headers": gateway_request.get("headers", {}),
                "body": gateway_request.get("body", {}),
            }
        },
    }


def _error(status_code: int, message: str) -> dict:
    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {
            "transformedGatewayResponse": {
                "statusCode": status_code,
                "headers": {},
                "body": {"error": message},
            }
        },
    }


# ─────────────────────────────────────────────────────────────────────────
# Handler
# ─────────────────────────────────────────────────────────────────────────
def lambda_handler(event, context):
    logger.info("Entra->bearer interceptor invoked.")

    if "mcp" not in event:
        return _error(400, "Invalid request structure.")

    # RESPONSE interception point (if you also register this as RESPONSE): pass through.
    if event["mcp"].get("gatewayResponse") is not None:
        gw = event["mcp"]["gatewayResponse"]
        return {
            "interceptorOutputVersion": "1.0",
            "mcp": {
                "transformedGatewayResponse": {
                    "statusCode": gw.get("statusCode", 200),
                    "headers": gw.get("headers", {}),
                    "body": gw.get("body", {}),
                }
            },
        }

    gateway_request = event["mcp"]["gatewayRequest"]
    headers = gateway_request.get("headers", {}).copy()
    body = gateway_request.get("body", {})
    tool_name = body.get("params", {}).get("name", "")
    logger.info("[Request] tool=%s", tool_name)

    # 1) Validate inbound Entra JWT. Requires passRequestHeaders=True on the gateway
    #    so the Authorization header reaches us.
    auth = headers.get("Authorization", "") or headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return _error(401, "No inbound Bearer token.")
    claims = validate_entra_jwt(auth[7:])
    if not claims:
        return _error(401, "Entra JWT validation failed.")

    # 2) email from claims
    email = email_from_claims(claims)
    if not email:
        return _error(403, f"No email claim ({EMAIL_CLAIM}) in token.")

    # 3) email -> special endpoint -> bearer (cached per email)
    bearer = mint_bearer_for_email(email)
    if not bearer:
        return _error(502, "Could not obtain downstream bearer token.")

    # 4) Replace the inbound Entra token with the 3rd-party bearer and forward.
    #    IMPORTANT: overwrite so the user's Entra token never leaks downstream.
    headers["Authorization"] = f"Bearer {bearer}"
    logger.info("[Auth] Downstream bearer injected for %s.", email)

    return _transformed_request(headers, body)
