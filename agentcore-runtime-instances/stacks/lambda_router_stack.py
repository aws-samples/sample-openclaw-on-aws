"""Lambda Router stack: Telegram webhook → AgentCore bridge.

Deploys:
- Lambda function (async webhook handler + worker)
- API Gateway HTTP API with POST /webhook/telegram route
- IAM role with bedrock-agentcore:InvokeAgentRuntime + lambda:InvokeFunction

Does NOT store the Telegram bot token — pass it as a parameter at deploy time
or store in Secrets Manager and reference via env var.
"""

from aws_cdk import (
    Duration,
    Stack,
    aws_apigatewayv2 as apigwv2,
    aws_iam as iam,
    aws_lambda as _lambda,
    CfnOutput,
)
from aws_cdk.aws_apigatewayv2_integrations import HttpLambdaIntegration
from constructs import Construct


class LambdaRouterStack(Stack):
    """Telegram webhook Lambda router for AgentCore Runtime."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        agent_runtime_arn: str,
        telegram_bot_token: str,
        webhook_secret_token: str,
        allowed_user_ids: str = "",
        session_id: str = "telegram-default-session",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Lambda execution role
        role = iam.Role(
            self,
            "RouterRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
        )

        # Permission to invoke AgentCore Runtime
        role.add_to_policy(
            iam.PolicyStatement(
                sid="InvokeAgentCore",
                actions=["bedrock-agentcore:InvokeAgentRuntime"],
                resources=[f"{agent_runtime_arn}*"],
            )
        )

        # Lambda function
        fn = _lambda.Function(
            self,
            "TelegramRouter",
            function_name="OpenClaw-TelegramRouter",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="index.handler",
            code=_lambda.Code.from_asset("lambda/router"),
            timeout=Duration.minutes(5),
            memory_size=256,
            role=role,
            environment={
                "AGENTCORE_RUNTIME_ARN": agent_runtime_arn,
                "SESSION_ID": session_id,
                "TELEGRAM_BOT_TOKEN": telegram_bot_token,
                "WEBHOOK_SECRET_TOKEN": webhook_secret_token,
                "ALLOWED_USER_IDS": allowed_user_ids,
            },
        )

        # Self-invoke permission (for async worker pattern)
        fn.grant_invoke(fn)

        # API Gateway HTTP API
        api = apigwv2.HttpApi(
            self,
            "WebhookApi",
            api_name="OpenClaw-TelegramWebhook",
            description="Telegram webhook endpoint for OpenClaw AgentCore",
        )

        # POST /webhook/telegram route
        integration = HttpLambdaIntegration("LambdaIntegration", fn)
        api.add_routes(
            path="/webhook/telegram",
            methods=[apigwv2.HttpMethod.POST],
            integration=integration,
        )

        # Outputs
        CfnOutput(
            self,
            "WebhookUrl",
            value=f"{api.url}webhook/telegram",
            description="Set this as the Telegram webhook URL",
        )
        CfnOutput(self, "FunctionArn", value=fn.function_arn)
        CfnOutput(self, "ApiEndpoint", value=api.api_endpoint)
