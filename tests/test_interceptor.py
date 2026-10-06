"""Unit tests for the interceptor's pure logic.

These exercise the parts that do NOT need AWS or network: output-shape helpers,
email-claim extraction, token-cache behaviour, and the handler's control flow
with JWT validation and the special-endpoint call stubbed out.

Run:  python3 -m pytest tests/ -q
"""
import importlib.util
import os
import sys
import time
import types
from pathlib import Path

import pytest

# ── Load the interceptor module with its env + heavy deps stubbed ────────
ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "src" / "interceptor" / "lambda_function.py"

os.environ.setdefault("ENTRA_TENANT_ID", "tenant-guid")
os.environ.setdefault("ENTRA_AUDIENCE", "api://app")
os.environ.setdefault("EMAIL_CLAIM", "email")
os.environ.setdefault("TOKEN_ENDPOINT", "https://special.example.com/token")
os.environ.setdefault("TOKEN_CACHE_TTL", "300")


def _stub(name, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


# Stub boto3 and jwt so import works without the real packages.
_stub("boto3", client=lambda *a, **k: types.SimpleNamespace())
_jwt = _stub(
    "jwt",
    decode=lambda *a, **k: {},
    ExpiredSignatureError=type("Exp", (Exception,), {}),
    InvalidTokenError=type("Inv", (Exception,), {}),
)
_stub("jwt.PyJWKClient")  # placeholder so `from jwt import PyJWKClient` resolves
_jwt.PyJWKClient = object  # type: ignore[attr-defined]


def load_module():
    spec = importlib.util.spec_from_file_location("interceptor_mod", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ic = load_module()


# ── output-shape helpers ─────────────────────────────────────────────────
def test_transformed_request_shape():
    out = ic._transformed_request({"Authorization": "Bearer x"}, {"a": 1})
    assert out["interceptorOutputVersion"] == "1.0"
    assert out["mcp"]["transformedGatewayRequest"]["headers"]["Authorization"] == "Bearer x"
    assert out["mcp"]["transformedGatewayRequest"]["body"] == {"a": 1}


def test_error_shape():
    out = ic._error(401, "nope")
    tr = out["mcp"]["transformedGatewayResponse"]
    assert tr["statusCode"] == 401
    assert tr["body"]["error"] == "nope"


# ── email extraction ─────────────────────────────────────────────────────
def test_email_from_primary_claim():
    assert ic.email_from_claims({"email": "a@b.com"}) == "a@b.com"


def test_email_falls_back_to_preferred_username():
    assert ic.email_from_claims({"preferred_username": "u@b.com"}) == "u@b.com"


def test_email_none_when_absent():
    assert ic.email_from_claims({"sub": "123"}) is None


# ── token cache ──────────────────────────────────────────────────────────
def test_token_cache_hit(monkeypatch):
    ic._TOKEN_CACHE.clear()
    ic._TOKEN_CACHE["a@b.com"] = {"token": "cached", "exp": time.time() + 100}
    # Should return cached without calling urlopen.
    monkeypatch.setattr(
        ic.urllib.request, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call")),
    )
    assert ic.mint_bearer_for_email("a@b.com") == "cached"


def test_token_cache_miss_calls_endpoint(monkeypatch):
    ic._TOKEN_CACHE.clear()

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            import json
            return json.dumps({"access_token": "fresh", "expires_in": 120}).encode()

    monkeypatch.setattr(ic.urllib.request, "urlopen", lambda *a, **k: FakeResp())
    monkeypatch.setattr(ic, "_endpoint_secret", lambda: {})
    tok = ic.mint_bearer_for_email("new@b.com")
    assert tok == "fresh"
    assert "new@b.com" in ic._TOKEN_CACHE


# ── handler control flow ─────────────────────────────────────────────────
def test_handler_rejects_without_mcp():
    out = ic.lambda_handler({}, None)
    assert out["mcp"]["transformedGatewayResponse"]["statusCode"] == 400


def test_handler_rejects_missing_bearer():
    event = {"mcp": {"gatewayRequest": {"headers": {}, "body": {}}}}
    out = ic.lambda_handler(event, None)
    assert out["mcp"]["transformedGatewayResponse"]["statusCode"] == 401


def test_handler_happy_path(monkeypatch):
    ic._TOKEN_CACHE.clear()
    monkeypatch.setattr(ic, "validate_entra_jwt", lambda t: {"email": "a@b.com"})
    monkeypatch.setattr(ic, "mint_bearer_for_email", lambda e: "downstream-bearer")
    event = {
        "mcp": {
            "gatewayRequest": {
                "headers": {"Authorization": "Bearer entra-jwt"},
                "body": {"params": {"name": "get_customer_data"}},
            }
        }
    }
    out = ic.lambda_handler(event, None)
    tr = out["mcp"]["transformedGatewayRequest"]
    assert tr["headers"]["Authorization"] == "Bearer downstream-bearer"


def test_handler_blocks_when_no_email(monkeypatch):
    monkeypatch.setattr(ic, "validate_entra_jwt", lambda t: {"sub": "x"})
    event = {
        "mcp": {
            "gatewayRequest": {
                "headers": {"Authorization": "Bearer entra-jwt"},
                "body": {},
            }
        }
    }
    out = ic.lambda_handler(event, None)
    assert out["mcp"]["transformedGatewayResponse"]["statusCode"] == 403
