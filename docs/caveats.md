# Caveats & verification status

What in this design is **documented by AWS** vs **inferred / not yet proven**. Based
on official AWS docs (links below). Read before taking this to production.

## Confirmed by AWS docs ✅

- **An interceptor can REPLACE the outbound `Authorization` header and the Gateway
  forwards it to the target.** *"The Authorization header from the interceptor
  lambda response is automatically propagated to the target... it will be forwarded
  to the target when provided by an interceptor lambda."* This is the backbone of
  this design.
  → https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-headers.html
- **`passRequestHeaders` defaults to `false`; set `true` to receive the inbound
  `Authorization` header** in the interceptor input payload.
  → https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-interceptors-configuration.html
- **REQUEST interceptor input/output shapes** used here (`mcp.gatewayRequest`,
  `interceptorOutputVersion:"1.0"`, `transformedGatewayRequest`,
  short-circuit via `transformedGatewayResponse`).
  → https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-interceptors-types.html
- **CUSTOM_JWT inbound authorizer is IdP-agnostic and accepts Entra/Azure AD** via a
  `discoveryUrl` ending in `/.well-known/openid-configuration`; requires at least one
  of `allowedAudience` / `allowedClients` / `allowedScopes`.
  → https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/inbound-jwt-authorizer.html
- **One REQUEST + one RESPONSE interceptor max; interceptors are Lambda-only.**
  → https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-interceptors.html

## Corrected from an earlier draft 🔧

- The AWS sample `sample-blog-implementing-custom-authentication-using-request-lambda-interceptor`
  uses a **Lambda** gateway target, **not** an OpenAPI target. (It still proves the
  interceptor pattern.) This repo now uses a **custom backend = API Gateway + Lambda**
  as the target — the sample's own shape — rather than an OpenAPI target pointed at a
  host we don't control.

## Inferred / NOT explicitly documented ⚠️ — verify before prod

- **Quick sending `Authorization: Bearer <token>` to the MCP endpoint on each
  `tools/call`.** Follows OAuth/MCP convention and Quick binds the per-user token to
  the MCP server (RFC 8707 Resource Indicators + PKCE), but the literal header is not
  spelled out in AWS docs. Quick also **does not support custom HTTP headers** — only
  the standard bearer path. **Verify by capturing a real token at the Gateway.**
  → https://docs.aws.amazon.com/quicksuite/latest/userguide/mcp-integration.html
- **No AWS reference demonstrates the full "Quick 3LO + custom IdP (Entra) → Gateway"
  combination end-to-end.** Each piece is supported individually; the published blog
  used **2LO + Cognito** for the Gateway example. The pieces fit per spec, but this
  exact tandem is unproven. **Treat first deploy as the proof.**

## Entra ID (Azure AD) gotchas 🔴

- **`AADSTS9010010`** — Entra v2.0 rejects RFC 8707 `resource` + `scope` together.
  Use **v1.0 endpoints** and set `accessTokenAcceptedVersion: 2` on the resource app.
- **`AADSTS90009`** — you generally need **two app registrations**: one *client* app
  (for Quick) and one *resource* app (the MCP server / `aud`). Don't point an app at
  itself.
- Make sure the resource app **emits the email claim** (optional claim `email`, or use
  `preferred_username` / `upn` and set `EMAIL_CLAIM` accordingly).

## Recommended verification order at first deploy

1. Deploy the stack; note `GatewayMcpUrl` and `BackendApiUrl`.
2. Register the Entra apps (client + resource); emit the email claim.
3. Create the Quick MCP Custom OAuth (3LO) integration pointed at `GatewayMcpUrl`.
4. From a Quick chat, invoke the tool. In the **interceptor's CloudWatch logs**,
   confirm: JWT validated, email extracted, bearer minted/cached, Authorization
   replaced. This single trace proves both ⚠️ items above at once.
5. Confirm the **backend** logs show the vendor call succeeding with the injected
   bearer.
