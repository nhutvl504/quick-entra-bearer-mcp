# Entra ID (2 app registrations) + Amazon Quick connector — setup guide

How to wire **Microsoft Entra ID** as the inbound IdP for this stack and point
**Amazon Quick** at the Gateway over 3LO. One Entra **user** is enough to demo; you
generally need **two app registrations** (a *client* app for Quick and a *resource*
app for the MCP server), because Entra rejects an app that is its own audience
(`AADSTS90009`).

> Values you capture here map 1:1 to `settings.py` / `.env`:
> `ENTRA_TENANT_ID`, `ENTRA_AUDIENCE`, `ENTRA_ALLOWED_CLIENT`, `EMAIL_CLAIM`.

---

## 0. Prerequisites

- An Entra tenant and one test user with an email (any member user).
- Permission to create App registrations (Application Administrator or similar).
- The stack deployed once, so you have the `GatewayMcpUrl` output (you can also set
  it up first and deploy after — the Quick step needs the Gateway URL).

Record your **Tenant ID**: Entra admin center → **Overview** → *Tenant ID*. This is
`ENTRA_TENANT_ID`.

---

## 1. Resource app registration (the MCP server / API — defines `aud`)

This app represents the protected resource. Its Application ID URI becomes the token
audience the Gateway checks.

1. Entra admin center → **App registrations** → **New registration**.
2. Name: `quick-entra-bearer-resource`. Supported account types: **Single tenant**.
   No redirect URI. Register.
3. Open the app → **Expose an API**:
   - Set **Application ID URI**. Accept the default `api://<app-id>` (or set a custom
     one). **This is `ENTRA_AUDIENCE`.**
   - **Add a scope**, e.g. `access_as_user`:
     - Who can consent: Admins and users.
     - Admin consent display name / description: anything, e.g. "Access MCP tools".
     - State: Enabled. Add.
   - The full scope string is `api://<app-id>/access_as_user` — you'll grant it to
     the client app in step 2.
4. (Token version) **Manifest** → ensure `accessTokenAcceptedVersion` is `2`
   (v2.0 tokens). This also avoids the `AADSTS9010010` error when a client sends an
   OAuth `resource` + `scope` together (RFC 8707).
5. **Note the resource app's Application (client) ID** — you may use it as
   `ENTRA_ALLOWED_CLIENT` is NOT this one; see step 2 for the client id.

---

## 2. Client app registration (what Amazon Quick signs in as)

This app is the OAuth client Quick uses for 3LO.

1. **App registrations** → **New registration**.
2. Name: `quick-entra-bearer-client`. Single tenant.
3. **Redirect URI**: platform **Web**, value:
   `https://<region>.quicksight.aws.amazon.com/sn/oauthcallback`
   (replace `<region>`, e.g. `us-east-1`). Register.
4. **Certificates & secrets** → **New client secret** → copy the **Value** now
   (shown once). This is the Quick "Client Secret".
5. **API permissions** → **Add a permission** → **My APIs** → select
   `quick-entra-bearer-resource` → **Delegated permissions** → check
   `access_as_user` → **Add permissions**. Then **Grant admin consent**.
6. **Token configuration** → **Add optional claim** → token type **Access** →
   check **email** → Add. If prompted, turn on the Microsoft Graph `email` permission.
   (If `email` is still empty at runtime, the interceptor falls back to
   `preferred_username` / `upn` — set `EMAIL_CLAIM` accordingly.)
7. **Note the client app's Application (client) ID.** This is `ENTRA_ALLOWED_CLIENT`
   and the Quick "Client ID".

---

## 3. Map the values into this stack

In `.env` (copied from `.env.example`):

```bash
export ENTRA_TENANT_ID="<tenant guid>"                 # step 0
export ENTRA_AUDIENCE="api://<resource-app-id>"        # step 1 Application ID URI
export ENTRA_ALLOWED_CLIENT="<client-app-id>"          # step 2 client id (optional pin)
export EMAIL_CLAIM="email"                             # or preferred_username / upn
```

The Gateway's `CUSTOM_JWT` authorizer will then accept a token whose `aud` =
`ENTRA_AUDIENCE` and (if set) whose `client_id`/`azp` = `ENTRA_ALLOWED_CLIENT`,
validating the signature against Entra's JWKS from:
`https://login.microsoftonline.com/<tenant>/discovery/v2.0/keys`.

Deploy / redeploy: `source .env && bash scripts/deploy.sh`. Note `GatewayMcpUrl`.

---

## 4. Amazon Quick — Custom OAuth app (3LO) integration

In Amazon Quick → **Integrations** → **Actions** → **Model Context Protocol** →
new integration:

| Field | Value |
|---|---|
| MCP server endpoint | the `GatewayMcpUrl` stack output |
| Authentication | **Custom OAuth app** (3LO) |
| Client ID | **client** app id (step 2.7) |
| Client Secret | client secret value (step 2.4) |
| Authorization URL | `https://login.microsoftonline.com/<tenant>/oauth2/v2.0/authorize` |
| Token URL | `https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token` |
| Redirect URL | `https://<region>.quicksight.aws.amazon.com/sn/oauthcallback` |
| Scope | `openid email profile api://<resource-app-id>/access_as_user` |

Create and continue. Quick runs the 3LO flow: your one test user signs in to Entra
and consents once; Quick stores that user's per-user token and refreshes it.

---

## 5. Verify (one user is enough)

From a Quick chat agent, invoke the `get_customer_data` tool. Then check CloudWatch:

- **Interceptor log group** — confirm the sequence:
  `JWT validated` → email extracted → bearer minted/cached → Authorization replaced.
- **Backend log group** — confirm the vendor call ran with the injected bearer.

If both show green for your single user, the whole per-user path works; a second user
would only demonstrate that each identity maps to its own bearer — not required.

---

## Troubleshooting (Entra `AADSTS`)

| Error | Cause | Fix |
|---|---|---|
| `AADSTS90009` | An app requesting a token for itself | Use the **two** app registrations above (client ≠ resource) |
| `AADSTS9010010` | v2.0 rejects RFC 8707 `resource` + `scope` together | Set resource app `accessTokenAcceptedVersion: 2`; use v2.0 endpoints |
| `aud` mismatch at Gateway | `ENTRA_AUDIENCE` ≠ token `aud` | Make `ENTRA_AUDIENCE` exactly the resource app's Application ID URI |
| email claim empty | optional claim not emitted | Add optional claim `email` (step 2.6), or set `EMAIL_CLAIM=preferred_username` |
| Gateway target rolls back at deploy | endpoint/credential wrong | Gateway calls `tools/list` at deploy; verify backend URL + that the interceptor injects a valid bearer |
