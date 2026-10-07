"""Main stack: AgentCore Gateway (inbound CUSTOM_JWT = Entra) + a REQUEST
Lambda interceptor that turns the per-user email claim into a per-user bearer
from a special endpoint, and a CUSTOM BACKEND (API Gateway + Lambda) as the target.

Why this shape:
  * Amazon Quick is the MCP client. It does 3LO with Entra ID and presents a
    per-user Entra JWT on the inbound hop. The Gateway authorizer is CUSTOM_JWT
    pointed at the Entra tenant, so only valid Entra tokens get in.
  * The vendor bearer is NOT a static key, so the sample's ApiKeyCredentialProvider
    cannot express it. Instead a REQUEST interceptor Lambda runs on every tool
    call: it reads the email claim, exchanges it at the special endpoint for a
    short-lived per-user bearer (cached by email), and REPLACES the Authorization
    header. AWS forwards the interceptor's Authorization header to the target
    (see docs/caveats.md -> gateway-headers).
  * The Gateway target is a CUSTOM BACKEND you own: API Gateway (REST) -> backend
    Lambda, which calls the real vendor API with the injected bearer. This is the
    AWS sample's own target shape (a Lambda/REST you own) rather than an OpenAPI
    target pointed straight at a host you don't control, and it lets the backend
    map tool args, normalise vendor responses, and keep the vendor URL server-side.

AgentCore resources are L1 (Cfn*) because there is no stable L2 yet. The service
validates the target at deploy time by calling tools/list, so a bad endpoint or a
rejected credential fails the deployment rather than 401ing in production.
"""
from __future__ import annotations

import json

from aws_cdk import (
    Aws,
    CfnOutput,
    Duration,
    Stack,
)
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from constructs import Construct

from settings import Settings


class QuickEntraBearerStack(Stack):
    def __init__(
        self, scope: Construct, construct_id: str, *, settings: Settings, **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.settings = settings

        backend_url = self._backend_api()
        interceptor = self._interceptor_lambda()
        gateway = self._gateway(interceptor)
        self._gateway_target(gateway, backend_url)

        CfnOutput(self, "GatewayMcpUrl", value=gateway.attr_gateway_url)
        CfnOutput(self, "GatewayArn", value=gateway.attr_gateway_arn)
        CfnOutput(self, "InterceptorArn", value=interceptor.function_arn)
        CfnOutput(self, "BackendApiUrl", value=backend_url)

    # ──────────────────────────────────────────────────────────────────
    # Custom backend: API Gateway (REST) -> backend Lambda. This is the
    # Gateway TARGET. The backend receives a request whose Authorization has
    # already been swapped by the interceptor to the per-user vendor bearer,
    # then calls the real vendor API. For the demo, the vendor is faked by
    # DEMO_THIRD_PARTY (below); for prod set THIRD_PARTY_BASE_URL to the vendor.
    # ──────────────────────────────────────────────────────────────────
    def _backend_api(self) -> str:
        backend = lambda_.Function(
            self,
            "BackendFn",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("src/backend"),
            timeout=Duration.seconds(20),
            memory_size=256,
            description="Gateway target backend: calls vendor API with injected bearer",
            environment={
                "THIRD_PARTY_BASE_URL": self.settings.third_party_base_url,
            },
        )

        api = apigw.LambdaRestApi(
            self,
            "BackendApi",
            handler=backend,
            proxy=False,
            rest_api_name="quick-entra-bearer-backend",
            deploy_options=apigw.StageOptions(
                stage_name="prod",
                logging_level=apigw.MethodLoggingLevel.ERROR,
            ),
            # The interceptor forwards Authorization automatically; no auth here
            # (this façade is only reachable from the Gateway target hop).
        )
        data = api.root.add_resource("data")
        data.add_method("POST")

        # api.url ends with a trailing slash; the Gateway target server URL is
        # the stage root, and the OpenAPI path "/data" appends to it.
        return api.url.rstrip("/")

    # ──────────────────────────────────────────────────────────────────
    # (Demo vendor API kept for local/standalone testing of the backend; it is
    # NOT wired as the Gateway target anymore. Point THIRD_PARTY_BASE_URL at it
    # to exercise the backend end-to-end without a real vendor.)
    # ──────────────────────────────────────────────────────────────────
    def _demo_third_party_api(self) -> lambda_.Function:
        fn = lambda_.Function(
            self,
            "DemoThirdPartyApi",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("src/third_party_api"),
            timeout=Duration.seconds(15),
            memory_size=128,
            description="Demo 3rd-party API: checks Bearer, echoes caller",
        )
        url = fn.add_function_url(auth_type=lambda_.FunctionUrlAuthType.NONE)
        fn.function_url = url.url  # type: ignore[attr-defined]
        return fn

    # ──────────────────────────────────────────────────────────────────
    # Interceptor Lambda: Entra JWT -> email -> special endpoint -> bearer.
    # ──────────────────────────────────────────────────────────────────
    def _interceptor_lambda(self) -> lambda_.Function:
        fn = lambda_.Function(
            self,
            "EntraBearerInterceptor",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="lambda_function.lambda_handler",
            # The asset dir must contain PyJWT[crypto]; scripts/build_interceptor.sh
            # vendors it next to lambda_function.py before `cdk deploy`.
            code=lambda_.Code.from_asset("src/interceptor"),
            timeout=Duration.seconds(30),
            memory_size=256,
            description="Entra email -> special endpoint -> per-user bearer",
            environment={
                "ENTRA_TENANT_ID": self.settings.entra_tenant_id,
                "ENTRA_AUDIENCE": self.settings.entra_audience,
                "EMAIL_CLAIM": self.settings.email_claim,
                "TOKEN_ENDPOINT": self.settings.special_token_endpoint,
                "TOKEN_ENDPOINT_SECRET_NAME": self.settings.special_endpoint_secret_name,
                "TOKEN_CACHE_TTL": self.settings.token_cache_ttl,
            },
        )
        # Only the gateway service may invoke the interceptor.
        fn.add_permission(
            "AllowGatewayInvoke",
            principal=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            action="lambda:InvokeFunction",
            source_account=Aws.ACCOUNT_ID,
        )
        # If the special endpoint needs its own credential, grant read on that
        # secret only (scoped by name, with the Secrets Manager -?????? suffix).
        if self.settings.special_endpoint_secret_name:
            fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["secretsmanager:GetSecretValue"],
                    resources=[
                        f"arn:aws:secretsmanager:{Aws.REGION}:{Aws.ACCOUNT_ID}:"
                        f"secret:{self.settings.special_endpoint_secret_name}-??????"
                    ],
                )
            )
        return fn

    # ──────────────────────────────────────────────────────────────────
    # Gateway with inbound CUSTOM_JWT (Entra) + REQUEST interceptor inline.
    # ──────────────────────────────────────────────────────────────────
    def _gateway(self, interceptor: lambda_.Function) -> agentcore.CfnGateway:
        role = iam.Role(
            self,
            "GatewayRole",
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": Aws.ACCOUNT_ID}},
            ),
            description="AgentCore Gateway service role",
        )
        self._gateway_role = role
        # Gateway role may invoke the interceptor Lambda (least privilege).
        interceptor.grant_invoke(role)

        allowed_clients = (
            [self.settings.entra_allowed_client]
            if self.settings.entra_allowed_client
            else None
        )

        gateway = agentcore.CfnGateway(
            self,
            "Gateway",
            name="quick-entra-bearer-dev",
            role_arn=role.role_arn,
            protocol_type="MCP",
            # Inbound auth = Entra ID JWT. Quick presents a per-user Entra token.
            authorizer_type="CUSTOM_JWT",
            authorizer_configuration=agentcore.CfnGateway.AuthorizerConfigurationProperty(
                custom_jwt_authorizer=agentcore.CfnGateway.CustomJWTAuthorizerConfigurationProperty(
                    discovery_url=self.settings.entra_discovery_url,
                    allowed_audience=[self.settings.entra_audience],
                    allowed_clients=allowed_clients,
                )
            ),
            # REQUEST interceptor; passRequestHeaders=True so it receives the
            # Authorization header carrying the inbound Entra JWT.
            interceptor_configurations=[
                agentcore.CfnGateway.GatewayInterceptorConfigurationProperty(
                    interception_points=["REQUEST"],
                    interceptor=agentcore.CfnGateway.InterceptorConfigurationProperty(
                        lambda_=agentcore.CfnGateway.LambdaInterceptorConfigurationProperty(
                            arn=interceptor.function_arn
                        )
                    ),
                    input_configuration=agentcore.CfnGateway.InterceptorInputConfigurationProperty(
                        pass_request_headers=True
                    ),
                )
            ],
            description="Quick -> 3rd-party MCP via Entra 3LO + bearer interceptor",
        )
        return gateway

    def _gateway_target(
        self, gateway: agentcore.CfnGateway, backend_url: str
    ) -> None:
        # OpenAPI target pointed at OUR backend (API Gateway), not the vendor.
        # NO credential provider: the interceptor injects Authorization, and the
        # backend forwards it to the vendor. The OpenAPI server URL is the API
        # Gateway stage root; "/data" appends to it.
        openapi = {
            "openapi": "3.0.0",
            "info": {"title": "Customer backend facade", "version": "1.0.0"},
            "servers": [{"url": backend_url}],
            "paths": {
                "/data": {
                    "post": {
                        "summary": "Fetch data as the signed-in user",
                        "operationId": "get_customer_data",
                        "requestBody": {
                            "required": False,
                            "content": {
                                "application/json": {
                                    "schema": {"type": "object"}
                                }
                            },
                        },
                        "responses": {
                            "200": {
                                "description": "Success",
                                "content": {
                                    "application/json": {
                                        "schema": {"type": "object"}
                                    }
                                },
                            }
                        },
                    }
                }
            },
        }

        target = agentcore.CfnGatewayTarget(
            self,
            "ThirdPartyTarget",
            gateway_identifier=gateway.attr_gateway_identifier,
            name="third-party-api",
            description="3rd-party API; interceptor mints per-user bearer",
            target_configuration=agentcore.CfnGatewayTarget.TargetConfigurationProperty(
                mcp=agentcore.CfnGatewayTarget.McpTargetConfigurationProperty(
                    open_api_schema=agentcore.CfnGatewayTarget.ApiSchemaConfigurationProperty(
                        inline_payload=json.dumps(openapi)
                    )
                )
            ),
            # No vendor credential provider: the interceptor injects Authorization.
            # The target still needs a provider entry; use the gateway IAM role.
            credential_provider_configurations=[
                agentcore.CfnGatewayTarget.CredentialProviderConfigurationProperty(
                    credential_provider_type="GATEWAY_IAM_ROLE"
                )
            ],
        )
        target.add_dependency(gateway)
