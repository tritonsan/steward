"""Synthesize with `python infra/app.py`; deployment is a separate explicit operation."""

import os
from datetime import datetime
from pathlib import Path

from aws_cdk import (
    App,
    CfnOutput,
    CfnParameter,
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import (
    aws_bedrockagentcore as agentcore,
)
from aws_cdk import (
    aws_cloudfront as cf,
)
from aws_cdk import (
    aws_cloudfront_origins as origins,
)
from aws_cdk import (
    aws_cognito as cognito,
)
from aws_cdk import (
    aws_ec2 as ec2,
)
from aws_cdk import (
    aws_ecr_assets as assets,
)
from aws_cdk import (
    aws_ecs as ecs,
)
from aws_cdk import (
    aws_elasticloadbalancingv2 as elb,
)
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk import (
    aws_logs as logs,
)
from aws_cdk import (
    aws_rds as rds,
)
from aws_cdk import (
    aws_s3 as s3,
)
from aws_cdk import (
    aws_s3_deployment as deploy,
)
from aws_cdk import (
    aws_secretsmanager as secrets,
)
from aws_cdk import (
    aws_ses as ses,
)
from aws_cdk import (
    aws_ses_actions as receipt_actions,
)
from aws_cdk import (
    aws_sns as sns,
)
from aws_cdk import (
    aws_sns_subscriptions as subscriptions,
)
from aws_cdk import (
    aws_sqs as sqs,
)
from aws_cdk import custom_resources as cr

ROOT = Path(__file__).resolve().parents[1]


class StewardStack(Stack):
    def __init__(self, scope, name):
        super().__init__(scope, name)
        mail_enabled = self.node.try_get_context("mailEnabled") != "false"
        mail_domain = CfnParameter(
            self,
            "MailDomain",
            type="String",
            default="site.narrativenode-labs.cloud",
            description="Owned SES-verified management domain with MX pointed to this region",
        )
        vendor_domain = CfnParameter(
            self,
            "ControlledVendorDomain",
            type="String",
            default="vendors.narrativenode-labs.cloud",
            description="Owned domain containing only the controlled vendor inboxes",
        )
        vpc = ec2.Vpc(
            self,
            "Network",
            max_azs=2,
            nat_gateways=1,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24
                ),
                ec2.SubnetConfiguration(
                    name="Application", subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS, cidr_mask=24
                ),
                ec2.SubnetConfiguration(
                    name="Database", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=24
                ),
            ],
        )
        database = rds.DatabaseInstance(
            self,
            "Operations",
            engine=rds.DatabaseInstanceEngine.postgres(
                version=rds.PostgresEngineVersion.of("16.15", "16")
            ),
            instance_type=ec2.InstanceType.of(ec2.InstanceClass.T4G, ec2.InstanceSize.MICRO),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
            database_name="steward",
            credentials=rds.Credentials.from_generated_secret("steward"),
            allocated_storage=20,
            max_allocated_storage=100,
            storage_encrypted=True,
            backup_retention=Duration.days(7),
            deletion_protection=True,
            removal_policy=RemovalPolicy.SNAPSHOT,
            publicly_accessible=False,
            cloudwatch_logs_exports=["postgresql"],
        )
        cluster = ecs.Cluster(
            self, "Cluster", vpc=vpc, container_insights_v2=ecs.ContainerInsights.ENABLED
        )
        application_uri = self.node.try_get_context("applicationImageUri")
        inference_uri = self.node.try_get_context("inferenceImageUri")
        application_image = (
            ecs.ContainerImage.from_registry(application_uri)
            if application_uri
            else ecs.ContainerImage.from_asset(str(ROOT), file="Dockerfile")
        )
        inference_image = (
            None
            if inference_uri
            else assets.DockerImageAsset(
                self,
                "InferenceImage",
                directory=str(ROOT),
                file="Dockerfile.agentcore",
                platform=assets.Platform.LINUX_ARM64,
            )
        )
        inference_role = iam.Role(
            self,
            "InferenceRole",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
        )
        if inference_image:
            inference_image.repository.grant_pull(inference_role)
            inference_uri = inference_image.image_uri
        else:
            inference_role.add_to_policy(
                iam.PolicyStatement(actions=["ecr:GetAuthorizationToken"], resources=["*"])
            )
            inference_role.add_to_policy(
                iam.PolicyStatement(
                    actions=["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
                    resources=[f"arn:aws:ecr:{self.region}:{self.account}:repository/steward-*"],
                )
            )
        inference_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=[f"arn:aws:bedrock:{self.region}::foundation-model/amazon.nova-pro-v1:0"],
            )
        )
        inference_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[
                    f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/bedrock-agentcore/*"
                ],
            )
        )
        inference = agentcore.CfnRuntime(
            self,
            "Inference",
            agent_runtime_name="steward_northgate_live_coordinator",
            agent_runtime_artifact={"containerConfiguration": {"containerUri": inference_uri}},
            network_configuration={"networkMode": "PUBLIC"},
            role_arn=inference_role.role_arn,
            environment_variables={"STEWARD_AWS_REGION": self.region},
        )
        # AgentCore validates the ECR role at creation; referencing RoleArn alone
        # doesn't wait for the separately created inline policy.
        inference.node.add_dependency(inference_role.node.find_child("DefaultPolicy"))
        archive = s3.Bucket(
            self,
            "MailArchive",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        dead = sqs.Queue(
            self,
            "MailDeadLetters",
            retention_period=Duration.days(14),
            enforce_ssl=True,
            encryption=sqs.QueueEncryption.SQS_MANAGED,
        )
        queue = sqs.Queue(
            self,
            "MailReceipts",
            visibility_timeout=Duration.minutes(5),
            retention_period=Duration.days(14),
            dead_letter_queue=sqs.DeadLetterQueue(queue=dead, max_receive_count=5),
            enforce_ssl=True,
            encryption=sqs.QueueEncryption.SQS_MANAGED,
        )
        topic = sns.Topic(self, "ReceiptMetadata")
        topic.add_subscription(subscriptions.SqsSubscription(queue))
        rules = None
        if mail_enabled:
            rules = ses.ReceiptRuleSet(self, "ReceiptRules")
            rules.add_rule(
                "StoreThenNotify",
                recipients=[mail_domain.value_as_string],
                scan_enabled=True,
                actions=[
                    receipt_actions.S3(bucket=archive, object_key_prefix="mail/"),
                    receipt_actions.Sns(topic=topic),
                ],
            )
            ses.EmailIdentity(
                self, "MailIdentity", identity=ses.Identity.domain(mail_domain.value_as_string)
            )
        configuration = ses.ConfigurationSet(self, "DeliveryConfiguration")
        members = secrets.Secret(
            self,
            "MemberRegistry",
            description="Cognito sub to registered actor and role mapping",
            secret_string_value=__import__("aws_cdk").SecretValue.unsafe_plain_text("{}"),
        )
        channels = secrets.Secret(
            self,
            "ChannelConfiguration",
            description="Telegram bot and webhook configuration",
            secret_string_value=__import__("aws_cdk").SecretValue.unsafe_plain_text("{}"),
        )
        pool = cognito.UserPool(
            self,
            "Members",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            mfa=cognito.Mfa.OPTIONAL,
            removal_policy=RemovalPolicy.RETAIN,
        )
        client = pool.add_client(
            "Web",
            generate_secret=False,
            auth_flows=cognito.AuthFlow(user_srp=True, admin_user_password=True),
            prevent_user_existence_errors=True,
        )
        domain = pool.add_domain(
            "SignIn",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=f"steward-live-{self.account}-{self.region}"
            ),
        )
        base_environment = {
            "STEWARD_AWS_REGION": self.region,
            "STEWARD_EXECUTION_MODE": "dry_run",
            "STEWARD_SIMULATION": "true",
            "STEWARD_SEMANTIC_MEMORY_ENABLED": "true",
            "STEWARD_AGENTCORE_RUNTIME_ARN": inference.attr_agent_runtime_arn,
            "STEWARD_MANAGEMENT_DOMAIN": mail_domain.value_as_string,
            "STEWARD_VENDOR_DOMAINS": vendor_domain.value_as_string,
            "STEWARD_SES_INBOUND_ENABLED": str(mail_enabled).lower(),
            "STEWARD_SES_INBOUND_BUCKET": archive.bucket_name,
            "STEWARD_SES_QUEUE_URL": queue.queue_url,
            "STEWARD_SES_CONFIGURATION_SET_NAME": configuration.configuration_set_name,
            "STEWARD_COGNITO_ISSUER": f"https://cognito-idp.{self.region}.amazonaws.com/{pool.user_pool_id}",
            "STEWARD_COGNITO_CLIENT_ID": client.user_pool_client_id,
        }
        telegram_delivery = self.node.try_get_context("telegramDeliveryMode") or "dry_run"
        if telegram_delivery not in ("dry_run", "live"):
            raise ValueError("telegramDeliveryMode must be dry_run or live")
        base_environment["STEWARD_TELEGRAM_DELIVERY_MODE"] = telegram_delivery
        base_environment["STEWARD_COGNITO_DOMAIN"] = (
            domain.domain_name + f".auth.{self.region}.amazoncognito.com"
        )
        if demo_clock := self.node.try_get_context("demoClock"):
            initial_time = datetime.fromisoformat(demo_clock)
            if initial_time.tzinfo is None:
                raise ValueError("demoClock must include an explicit timezone")
            base_environment["STEWARD_SIMULATION_CLOCK_START"] = initial_time.isoformat()

        def task(name, command):
            definition = ecs.FargateTaskDefinition(
                self, name + "Task", cpu=512, memory_limit_mib=1024
            )
            group = logs.LogGroup(
                self,
                name + "Logs",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.RETAIN,
            )
            container = definition.add_container(
                name,
                image=application_image,
                command=command,
                environment=base_environment,
                secrets={
                    "STEWARD_DATABASE_SECRET": ecs.Secret.from_secrets_manager(database.secret),
                    "STEWARD_MEMBERS": ecs.Secret.from_secrets_manager(members),
                    "STEWARD_CHANNEL_CONFIGURATION": ecs.Secret.from_secrets_manager(channels),
                },
                logging=ecs.LogDrivers.aws_logs(stream_prefix=name, log_group=group),
            )
            definition.add_to_execution_role_policy(
                iam.PolicyStatement(actions=["ecr:GetAuthorizationToken"], resources=["*"])
            )
            definition.add_to_execution_role_policy(
                iam.PolicyStatement(
                    actions=[
                        "ecr:BatchCheckLayerAvailability",
                        "ecr:BatchGetImage",
                        "ecr:GetDownloadUrlForLayer",
                    ],
                    resources=[f"arn:aws:ecr:{self.region}:{self.account}:repository/steward-*"],
                )
            )
            definition.add_to_task_role_policy(
                iam.PolicyStatement(
                    actions=["bedrock-agentcore:InvokeAgentRuntime"],
                    resources=[
                        inference.attr_agent_runtime_arn,
                        inference.attr_agent_runtime_arn + "/runtime-endpoint/*",
                    ],
                )
            )
            definition.add_to_task_role_policy(
                iam.PolicyStatement(
                    actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                    resources=[
                        f"arn:aws:bedrock:{self.region}::foundation-model/amazon.nova-pro-v1:0",
                        f"arn:aws:bedrock:{self.region}::foundation-model/amazon.titan-embed-text-v2:0",
                    ],
                )
            )
            definition.add_to_task_role_policy(
                iam.PolicyStatement(
                    actions=["ses:SendEmail", "ses:SendRawEmail"],
                    resources=[
                        f"arn:aws:ses:{self.region}:{self.account}:identity/{mail_domain.value_as_string}",
                        f"arn:aws:ses:{self.region}:{self.account}:configuration-set/{configuration.configuration_set_name}",
                    ],
                )
            )
            archive.grant_read(definition.task_role)
            queue.grant_consume_messages(definition.task_role)
            return definition, container

        api_task, api_container = task("Api", None)
        api_container.add_port_mappings(ecs.PortMapping(container_port=8000))
        api_service = ecs.FargateService(
            self,
            "ApiService",
            cluster=cluster,
            task_definition=api_task,
            desired_count=1,
            min_healthy_percent=100,
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
        )
        worker_task, worker_container = task("Worker", ["steward-worker"])
        worker = ecs.FargateService(
            self,
            "WorkerService",
            cluster=cluster,
            task_definition=worker_task,
            desired_count=1,
            min_healthy_percent=100,
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
        )
        database.connections.allow_default_port_from(api_service)
        database.connections.allow_default_port_from(worker)
        alb = elb.ApplicationLoadBalancer(self, "ApiOrigin", vpc=vpc, internet_facing=False)
        listener = alb.add_listener("Http", port=80, open=False)
        listener.add_targets(
            "Api",
            port=8000,
            targets=[api_service],
            health_check=elb.HealthCheck(path="/health"),
            deregistration_delay=Duration.seconds(30),
        )
        website = s3.Bucket(
            self,
            "Website",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        distribution = cf.Distribution(
            self,
            "Console",
            default_root_object="index.html",
            default_behavior=cf.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(website),
                viewer_protocol_policy=cf.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            ),
            additional_behaviors={
                "api/*": cf.BehaviorOptions(
                    origin=origins.VpcOrigin.with_application_load_balancer(
                        alb,
                        protocol_policy=cf.OriginProtocolPolicy.HTTP_ONLY,
                        read_timeout=Duration.seconds(60),
                    ),
                    cache_policy=cf.CachePolicy.CACHING_DISABLED,
                    origin_request_policy=cf.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                    allowed_methods=cf.AllowedMethods.ALLOW_ALL,
                    viewer_protocol_policy=cf.ViewerProtocolPolicy.HTTPS_ONLY,
                )
            },
        )
        cloudfront_group_lookup = cr.AwsCustomResource(
            self,
            "CloudFrontSourceGroup",
            on_update=cr.AwsSdkCall(
                service="EC2",
                action="describeSecurityGroups",
                parameters={
                    "Filters": [
                        {"Name": "vpc-id", "Values": [vpc.vpc_id]},
                        {"Name": "group-name", "Values": ["CloudFront-VPCOrigins-Service-SG"]},
                    ]
                },
                physical_resource_id=cr.PhysicalResourceId.of("StewardCloudFrontSourceGroup"),
            ),
            policy=cr.AwsCustomResourcePolicy.from_sdk_calls(
                resources=cr.AwsCustomResourcePolicy.ANY_RESOURCE
            ),
            install_latest_aws_sdk=False,
        )
        cloudfront_group_lookup.node.add_dependency(distribution)
        ec2.CfnSecurityGroupIngress(
            self,
            "CloudFrontToPrivateApi",
            group_id=alb.connections.security_groups[0].security_group_id,
            source_security_group_id=cloudfront_group_lookup.get_response_field(
                "SecurityGroups.0.GroupId"
            ),
            ip_protocol="tcp",
            from_port=80,
            to_port=80,
            description="CloudFront managed VPC origin to private API",
        )
        deploy.BucketDeployment(
            self,
            "PublishConsole",
            sources=[deploy.Source.asset(str(ROOT / "web" / "dist"))],
            destination_bucket=website,
            distribution=distribution,
            distribution_paths=["/*"],
            prune=False,
        )
        client_resource = client.node.default_child
        client_resource.allowed_o_auth_flows = ["code"]
        client_resource.allowed_o_auth_flows_user_pool_client = True
        client_resource.allowed_o_auth_scopes = ["openid", "email", "profile"]
        client_resource.callback_ur_ls = ["https://" + distribution.distribution_domain_name + "/"]
        client_resource.supported_identity_providers = ["COGNITO"]
        api_container.add_environment(
            "STEWARD_PUBLIC_URL", "https://" + distribution.distribution_domain_name
        )
        worker_container.add_environment(
            "STEWARD_PUBLIC_URL", "https://" + distribution.distribution_domain_name
        )
        CfnOutput(self, "ConsoleUrl", value="https://" + distribution.distribution_domain_name)
        CfnOutput(self, "MemberRegistrySecret", value=members.secret_arn)
        if rules:
            CfnOutput(self, "ReceiptRuleSetToActivate", value=rules.receipt_rule_set_name)
        CfnOutput(self, "DatabaseSecret", value=database.secret.secret_arn)
        CfnOutput(self, "InferenceArn", value=inference.attr_agent_runtime_arn)
        CfnOutput(self, "UserPoolId", value=pool.user_pool_id)
        CfnOutput(self, "UserPoolClientId", value=client.user_pool_client_id)
        CfnOutput(self, "ChannelConfigurationSecret", value=channels.secret_arn)
        CfnOutput(self, "ClusterName", value=cluster.cluster_name)
        CfnOutput(self, "ApiServiceName", value=api_service.service_name)
        CfnOutput(self, "WorkerServiceName", value=worker.service_name)


app = App(outdir=os.environ.get("CDK_OUTDIR", str(ROOT / "infra" / "cdk.out")))
StewardStack(app, "StewardNorthgateLive")
app.synth()
