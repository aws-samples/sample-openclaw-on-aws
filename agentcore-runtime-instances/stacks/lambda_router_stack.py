"""Lambda Router stack: Multi-channel webhook -> AgentCore bridge.

Deploys:
- Lambda function (async webhook handler + worker, multi-channel)
- API Gateway HTTP API with routes for each channel
- DynamoDB table for cold-start tracking
- IAM role with AgentCore invoke + Lambda self-invoke + DynamoDB access

Channels supported: Telegram, Discord, Slack (extensible)
"""

from aws_cdk import (
    BundlingOptions,
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigatewayv2 as apigwv2,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as _lambda,
    CfnOutput,
)
from aws_cdk.aws_apigatewayv2_integrations import HttpLambdaIntegration
from constructs import Construct


class LambdaRouterStack(Stack):
    """Multi-channel webhook Lambda router for AgentCore Runtime."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        agent_runtime_arn: str,
        telegram_bot_token: str = "",
        webhook_secret_token: str = "",
        allowed_user_ids: str = "",
        session_id: str = "default-session",
        discord_bot_token: str = "",
        discord_public_key: str = "",
        discord_application_id: str = "",
        discord_allowed_user_ids: str = "",
        slack_bot_token: str = "",
        slack_signing_secret: str = "",
        slack_allowed_user_ids: str = "",
        idle_timeout_seconds: int = 900,
        reserved_concurrent_executions: int = 8,
        throttle_burst_limit: int = 10,
        throttle_rate_limit: int = 5,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # DynamoDB table for cold-start tracking
        table = dynamodb.Table(
            self,
            "ColdStartTable",
            table_name="OpenClaw-RouterColdStart",
            partition_key=dynamodb.Attribute(
                name="session_id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

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
            "Router",
            function_name="OpenClaw-TelegramRouter",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="index.handler",
            code=_lambda.Code.from_asset(
                "lambda/router",
                bundling=BundlingOptions(
                    image=_lambda.Runtime.PYTHON_3_12.bundling_image,
                    command=[
                        "bash",
                        "-c",
                        "pip install -r requirements.txt -t /asset-output --upgrade "
                        "--no-cache-dir && cp -r . /asset-output",
                    ],
                ),
            ),
            timeout=Duration.minutes(5),
            memory_size=256,
            role=role,
            reserved_concurrent_executions=reserved_concurrent_executions,
            environment={
                "AGENTCORE_RUNTIME_ARN": agent_runtime_arn,
                "SESSION_ID": session_id,
                "IDLE_TIMEOUT_SECONDS": str(idle_timeout_seconds),
                "COLDSTART_TABLE": table.table_name,
                # Telegram
                "TELEGRAM_BOT_TOKEN": telegram_bot_token,
                "WEBHOOK_SECRET_TOKEN": webhook_secret_token,
                "ALLOWED_USER_IDS": allowed_user_ids,
                # Discord
                "DISCORD_BOT_TOKEN": discord_bot_token,
                "DISCORD_PUBLIC_KEY": discord_public_key,
                "DISCORD_APPLICATION_ID": discord_application_id,
                "DISCORD_ALLOWED_USER_IDS": discord_allowed_user_ids,
                # Slack
                "SLACK_BOT_TOKEN": slack_bot_token,
                "SLACK_SIGNING_SECRET": slack_signing_secret,
                "SLACK_ALLOWED_USER_IDS": slack_allowed_user_ids,
            },
        )

        # Self-invoke permission (async worker pattern)
        fn.grant_invoke(fn)

        # DynamoDB permissions
        table.grant_read_write_data(fn)

        # API Gateway HTTP API
        api = apigwv2.HttpApi(
            self,
            "WebhookApi",
            api_name="OpenClaw-TelegramWebhook",
            description="Multi-channel webhook endpoint for OpenClaw AgentCore",
        )

        # Throttle the default stage to bound worst-case cost from an
        # unauthenticated/abusive request volume (defense in depth alongside
        # channel auth + reserved concurrency above).
        cfn_stage = api.default_stage.node.default_child
        cfn_stage.default_route_settings = apigwv2.CfnStage.RouteSettingsProperty(
            throttling_burst_limit=throttle_burst_limit,
            throttling_rate_limit=throttle_rate_limit,
        )

        integration = HttpLambdaIntegration("LambdaIntegration", fn)

        # Routes -- only registered for channels with credentials configured.
        # An unconfigured channel should have no route at all, not merely an
        # unauthenticated one that fails validate_webhook() at request time.
        channel_enabled = {
            "telegram": bool(telegram_bot_token),
            "discord": bool(discord_bot_token and discord_public_key),
            "slack": bool(slack_bot_token and slack_signing_secret),
        }
        for channel, enabled in channel_enabled.items():
            if not enabled:
                continue
            api.add_routes(
                path=f"/webhook/{channel}",
                methods=[apigwv2.HttpMethod.POST],
                integration=integration,
            )

        # Outputs -- only for channels actually enabled/routed.
        if channel_enabled["telegram"]:
            CfnOutput(self, "TelegramWebhookUrl",
                      value=f"{api.url}webhook/telegram")
        if channel_enabled["discord"]:
            CfnOutput(self, "DiscordWebhookUrl",
                      value=f"{api.url}webhook/discord")
        if channel_enabled["slack"]:
            CfnOutput(self, "SlackWebhookUrl",
                      value=f"{api.url}webhook/slack")
        CfnOutput(self, "FunctionArn", value=fn.function_arn)
        CfnOutput(self, "ColdStartTableName", value=table.table_name)
