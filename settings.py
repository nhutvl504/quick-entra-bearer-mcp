"""Deployment configuration, loaded from environment variables.

Nothing here is a secret. The special-endpoint credential (if any) lives in a
Secrets Manager secret you create out-of-band; see scripts/create_secret.sh.

Set these before `cdk deploy` (or export them in your shell / CI):

  ENTRA_TENANT_ID      Azure AD tenant GUID.
  ENTRA_AUDIENCE       Expected 'aud' of the inbound token = your Entra app's
                       Application ID URI (e.g. api://<app-id>).
  ENTRA_ALLOWED_CLIENT (optional) Entra app client id to pin as allowed client.
  EMAIL_CLAIM          Claim holding the email (default "email"; Entra often
                       uses "preferred_username" or "upn").
  THIRD_PARTY_BASE_URL Base URL of the customer's 3rd-party REST API.
  SPECIAL_TOKEN_ENDPOINT  URL that exchanges {email} -> {access_token}.
  SPECIAL_ENDPOINT_SECRET_NAME (optional) Secrets Manager secret name holding a
                       credential the special endpoint itself needs.
  TOKEN_CACHE_TTL      Seconds to cache a minted bearer per email (default 300).
"""
from __future__ import annotations

import os
from dataclasses import dataclass


class SettingsError(RuntimeError):
    """Raised when a required setting is missing."""


def _require(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise SettingsError(
            f"Missing required environment variable {name!r}. "
            f"See settings.py docstring and .env.example."
        )
    return val


@dataclass(frozen=True)
class Settings:
    entra_tenant_id: str
    entra_audience: str
    entra_allowed_client: str
    email_claim: str
    third_party_base_url: str
    special_token_endpoint: str
    special_endpoint_secret_name: str
    token_cache_ttl: str

    @classmethod
    def load(cls) -> "Settings":
        return cls(
            entra_tenant_id=_require("ENTRA_TENANT_ID"),
            entra_audience=_require("ENTRA_AUDIENCE"),
            entra_allowed_client=os.environ.get("ENTRA_ALLOWED_CLIENT", "").strip(),
            email_claim=os.environ.get("EMAIL_CLAIM", "email").strip(),
            third_party_base_url=_require("THIRD_PARTY_BASE_URL"),
            special_token_endpoint=_require("SPECIAL_TOKEN_ENDPOINT"),
            special_endpoint_secret_name=os.environ.get(
                "SPECIAL_ENDPOINT_SECRET_NAME", ""
            ).strip(),
            token_cache_ttl=os.environ.get("TOKEN_CACHE_TTL", "300").strip(),
        )

    @property
    def entra_issuer(self) -> str:
        return f"https://login.microsoftonline.com/{self.entra_tenant_id}/v2.0"

    @property
    def entra_discovery_url(self) -> str:
        return (
            f"https://login.microsoftonline.com/{self.entra_tenant_id}"
            f"/v2.0/.well-known/openid-configuration"
        )
