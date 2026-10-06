#!/usr/bin/env python3
"""CDK app entrypoint — Amazon Quick (3LO Entra) -> AgentCore Gateway ->
Request Lambda Interceptor (email -> per-user bearer) -> 3rd-party API."""
import os

import aws_cdk as cdk

from infra.stack import QuickEntraBearerStack
from settings import Settings

app = cdk.App()

settings = Settings.load()

QuickEntraBearerStack(
    app,
    "quick-entra-bearer-mcp-dev",
    settings=settings,
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
    ),
)

app.synth()
