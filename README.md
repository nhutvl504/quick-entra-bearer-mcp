# quick-entra-bearer-mcp

Host a **third-party MCP/REST API** behind **Amazon Bedrock AgentCore Gateway**, where:

- the client is **Amazon Quick** (MCP Actions connector),
- login is **Entra ID (Azure AD)** via **3LO** — Quick does the OAuth, so every
  tool call carries a **per-user** Entra JWT,
- the third-party API is NOT reached with a static key. Instead a **Request Lambda
  Interceptor** reads the user's **email** claim, calls a **special endpoint** to
  mint a **per-user bearer token**, caches it, and injects it on the outbound hop.

The agent / Quick never sees the bearer token — it is minted and injected inside
the Gateway hop, so a prompt-injected model cannot exfiltrate it.

> Deploy-ready CDK (Python). Self-contained demo: a stand-in 3rd-party API Lambda
> lets you exercise the whole path without a real vendor.


Full sequence + per-field config: [`docs/sequence.md`](docs/sequence.md).

Deployment topology (what runs where, trust boundaries): [`docs/topology.md`](docs/topology.md).


## Architecture

```
Amazon Quick  --3LO Entra, per-user JWT-->  AgentCore Gateway (inbound CUSTOM_JWT = Entra)
                                                   |
                                   REQUEST Lambda Interceptor (passRequestHeaders=true)
                                   1. validate Entra JWT (JWKS cache)
                                   2. read email claim
                                   3. email -> special endpoint -> bearer (cached per email, TTL)
                                   4. REPLACE Authorization: Bearer <3rd>
                                                   |
                                   Gateway Target = OUR backend  (NO vendor credential)
                                                   |
                                   API Gateway (REST) -> Backend Lambda
                                                   |
                                   3rd-party API  (called with the injected bearer)
```

The Gateway target is a **custom backend you own** (API Gateway + Lambda), not an
OpenAPI target pointed straight at the vendor. The interceptor replaces the
`Authorization` header with the per-user vendor bearer, the Gateway forwards it to
the backend (AWS documents this — see [`docs/caveats.md`](docs/caveats.md)), and the
backend calls the real vendor API. This is the AWS sample's own target shape (a
Lambda/REST you own) and keeps the vendor URL server-side.

Why a Request Interceptor and not an `ApiKeyCredentialProvider`: the vendor bearer
is **dynamic and per-user**, derived at call time from the signed-in user's email.
A static credential provider cannot express that. The interceptor is custom code
that runs on every tool call, so it can do the email→token exchange and keep the
token off the agent. (Pattern from the AWS sample
*Custom Authentication with Request Lambda Interceptor*.)

Why **3LO** (not 2LO): we need the **email of the user who is chatting**. 2LO
(client-credentials) runs as a shared service account and carries no user identity.
3LO makes each user sign in to Entra once, so the token carries their email.

## Repository layout

```
app.py                      CDK entrypoint
settings.py                 env-driven config (nothing secret here)
infra/stack.py              THE stack: Gateway (CUSTOM_JWT=Entra) + interceptor + backend(API GW+Lambda) target
src/interceptor/
  lambda_function.py        the Request Interceptor (Entra -> email -> bearer)
src/third_party_api/
  handler.py                demo 3rd-party API (local stand-in for the vendor)
src/backend/
  handler.py                backend behind API Gateway = the Gateway target; calls vendor with injected bearer
scripts/
  build_interceptor.sh      vendor PyJWT[crypto] into the interceptor asset
  create_secret.sh          (optional) secret for the special endpoint's own credential
  deploy.sh                 build + synth + deploy
tests/test_interceptor.py   unit tests for the interceptor logic (no AWS needed)
docs/sequence.md            the full sequence diagram (Mermaid)
docs/topology.md            deployment topology (Mermaid)
```

## Prerequisites

- AWS account with Bedrock AgentCore Gateway + Identity available, CDK bootstrapped.
- Python 3.13+ locally (the Lambda runtime is 3.13), Node.js 20+, Docker not required.
- An Entra ID (Azure AD) app registration (see below).
- A **special endpoint** that exchanges `{ email }` → `{ access_token, expires_in }`.
- The third-party API URL (or use the demo API this stack deploys).

## Configure

```bash
cp .env.example .env     # fill in your values
source .env
```

Key fields (full list in `.env.example` / `settings.py`):

| Var | Meaning |
|---|---|
| `ENTRA_TENANT_ID` | Azure AD tenant GUID |
| `ENTRA_AUDIENCE` | Application ID URI of your Entra app (the token `aud`) |
| `EMAIL_CLAIM` | where Entra puts the email (`email` / `preferred_username` / `upn`) |
| `THIRD_PARTY_BASE_URL` | the vendor API base URL (or the demo API URL after first deploy) |
| `SPECIAL_TOKEN_ENDPOINT` | endpoint that turns `{email}` into a bearer |
| `SPECIAL_ENDPOINT_SECRET_NAME` | (optional) Secrets Manager secret the endpoint needs |

## Deploy

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
source .env
bash scripts/deploy.sh
```

`deploy.sh` vendors PyJWT into the interceptor asset, synthesizes, and deploys.
Outputs: `GatewayMcpUrl`, `GatewayArn`, `InterceptorArn`, `BackendApiUrl`.

> The Gateway target validates its endpoint at deploy time by calling `tools/list`.
> A wrong URL or a rejected credential **fails the deployment** rather than 401ing
> in production — verify your endpoint first.

## Wire Amazon Quick (Custom OAuth app = Entra, 3LO)

In Amazon Quick → Integrations → Actions → Model Context Protocol → new integration:

- **MCP server endpoint** = the `GatewayMcpUrl` output.
- **Authentication** = Custom OAuth app (3LO):
  - Client ID / Secret = your Entra app registration
  - Authorization URL = `https://login.microsoftonline.com/<tenant>/oauth2/v2.0/authorize`
  - Token URL = `https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token`
  - Redirect URL = `https://<region>.quicksight.aws.amazon.com/sn/oauthcallback`
  - Scope = `openid email profile` + your Gateway API scope

Entra app registration must: add that redirect URI, expose an API scope (so the
`aud` matches `ENTRA_AUDIENCE`), and emit the `email` claim.

## Test the path end-to-end (demo)

The Gateway target is the backend (API Gateway + Lambda); the backend calls whatever
`THIRD_PARTY_BASE_URL` points at. To exercise it without a real vendor, deploy the
demo vendor Lambda and set `THIRD_PARTY_BASE_URL` to its Function URL, then from a
Quick chat invoke the `get_customer_data` tool. Confirm in CloudWatch:
interceptor logs show the Authorization header replaced with the per-user bearer, and
the backend logs show the vendor call succeeding with that bearer.

See [`docs/caveats.md`](docs/caveats.md) for what is AWS-documented vs. to-verify,
and the Entra `AADSTS` gotchas.

## Run unit tests

```bash
pip install pytest
python3 -m pytest tests/ -q
```

The tests stub AWS + network and cover: output-shape helpers, email extraction,
token-cache hit/miss, and the handler control flow (missing bearer → 401, no email
→ 403, happy path injects `Bearer <downstream>`).

## Teardown

```bash
npx aws-cdk@latest destroy quick-entra-bearer-mcp-dev
```

## Security notes

- The downstream bearer never reaches Quick or the model — minted + injected in the
  interceptor on the Gateway→vendor hop.
- Inbound is Entra JWT (CUSTOM_JWT authorizer); only valid Entra tokens get in.
- The interceptor Lambda is invokable only by the Gateway service principal.
- If the special endpoint needs its own credential, it lives in Secrets Manager and
  only the interceptor role can read it (scoped by name).
- Make the interceptor idempotent (the Gateway may retry); the per-email cache does
  this and avoids minting a token on every call.

## License

MIT-0.
