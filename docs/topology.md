# Deployment topology — quick-entra-bearer-mcp

Component view of the deployed resources and the trust boundaries. Pairs with the
message-level [`sequence.md`](sequence.md): the sequence shows the ordered calls,
this shows *what runs where* and *who owns each boundary*.

![topology](topology.png)

## Boundaries

- **External actors / SaaS** (left): the user's browser and **Amazon Quick** (the
  MCP client), plus **Entra ID** as the identity provider. None of these is in your
  AWS account.
- **AWS account (us-east-1)**: everything this CDK stack deploys, split into two
  trust zones.
  - **Amazon Bedrock AgentCore (managed)**: the Gateway, its Target, and AgentCore
    Identity / Token Vault. AWS runs these; you configure them.
  - **Your resources (CDK stack)**: the interceptor Lambda, the demo 3rd-party API
    Lambda (+ Function URL), the optional Secrets Manager secret, and CloudWatch
    Logs. These are the resources `infra/stack.py` creates.
- **Customer side (vendor)**: the **special endpoint** (`email → bearer`) and the
  real **3rd-party API**. Outside your account; reached over the public internet.
  In the demo, the Gateway target points at the demo API Lambda instead.

## Flows (numbered to match the diagram arrows)

1. **User → Quick** — the user drives Amazon Quick.
2. **Quick ↔ Entra ID (3LO)** — Quick runs the OAuth authorization-code flow with
   Entra; the user signs in once and Quick stores a **per-user** token (claim: email),
   refreshing it (90-day refresh token).
3. **Quick → Gateway** — Quick's MCP client calls `tools/call` with the per-user
   Entra JWT in the Authorization header. Gateway inbound authorizer `CUSTOM_JWT`
   verifies issuer + audience against the Entra tenant.
4. **Gateway → Interceptor (REQUEST, passRequestHeaders)** — on every tool call the
   Gateway invokes the interceptor Lambda, forwarding request headers.
5. **Interceptor → Secrets Manager** *(optional)* — read a credential the special
   endpoint itself requires.
6. **Interceptor → Special endpoint** — `POST { email }` → `{ access_token }`,
   cached per email (TTL) so it is not minted on every call.
7. **Interceptor → Gateway (inject)** — returns the transformed request with
   `Authorization: Bearer <downstream>`.
8. **Gateway → 3rd-party API** — the paid/real call, carrying the downstream bearer.
9. **Interceptor → CloudWatch Logs** — allowlisted operational logs (no tokens).

## Key security properties the topology makes visible

- The **downstream bearer** is minted and injected **inside the interceptor**, in
  your account — it never crosses back to Quick or the model.
- **Inbound** is Entra JWT only (`CUSTOM_JWT`); **outbound** credential handling is
  isolated in the interceptor + Secrets Manager, not in the agent or the connector.
- The interceptor Lambda is invokable **only** by the AgentCore Gateway service
  principal; the Secrets Manager read is scoped to one secret by name.
- The Gateway **Target carries no vendor credential** (`GATEWAY_IAM_ROLE`) — all
  downstream auth is the interceptor's job.

## Production vs demo

| | Demo (this repo as-is) | Production |
|---|---|---|
| 3rd-party API | Demo Lambda + Function URL in your account | Real vendor URL (`THIRD_PARTY_BASE_URL`) |
| Special endpoint | Your real endpoint (always external) | same |
| Networking | Function URL is public | Put the vendor call behind egress controls; add WAF / spend limits as needed |
