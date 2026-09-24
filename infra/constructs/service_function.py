"""One AERA Lambda component (SRD 6.15, 6.16, 6.22): `aera-{env}-{component}`, Python 3.12
on ARM64, X-Ray tracing, 30-day logs, and read access to its `/aera/{env}/` parameters.

The code asset is the Lambda bundle built by `scripts/build_lambda.py` (the `services`
package plus runtime dependencies). Without a bundle, the bare `services` source is used so
the app still synthesises for tests; `scripts/deploy_dev.py` refuses to deploy that.
"""

from pathlib import Path
from typing import Any

from aws_cdk import ArnFormat, Duration, RemovalPolicy, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct

ROOT = Path(__file__).resolve().parents[2]


def service_code(bundle: str | None) -> lambda_.Code:
    if bundle:
        path = Path(bundle)
        if not (path / "services").is_dir():
            raise ValueError(f"{bundle} is not a Lambda bundle; run scripts/build_lambda.py")
        return lambda_.Code.from_asset(str(path))
    # Synthesis only (tests, `cdk synth`): the handlers' source without dependencies.
    return lambda_.Code.from_asset(str(ROOT / "services"), exclude=["**/tests", "**/__pycache__"])


class ServiceFunction(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        env_name: str,
        component: str,
        code: lambda_.Code,
        environment: dict[str, str] | None = None,
        memory_mb: int = 512,
        timeout: Duration | None = None,
        reserved_concurrency: int | None = None,
        secrets: tuple[str, ...] = (),
        module: str | None = None,
        parameters: tuple[str, ...] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id)
        stack = Stack.of(self)
        module = module or component.replace("-", "_")
        log_group = logs.LogGroup(
            self,
            "Logs",
            log_group_name=f"/aws/lambda/aera-{env_name}-{component}",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.function = lambda_.Function(
            self,
            "Function",
            function_name=f"aera-{env_name}-{component}",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler=f"services.{module}.handler.lambda_handler",
            code=code,
            memory_size=memory_mb,
            timeout=timeout or Duration.seconds(30),
            tracing=lambda_.Tracing.ACTIVE,
            log_group=log_group,
            reserved_concurrent_executions=reserved_concurrency,
            environment={
                "AERA_ENV": env_name,
                "POWERTOOLS_SERVICE_NAME": f"aera-{component}",
                **(environment or {}),
            },
            **kwargs,
        )
        # Only the named parameters when given (NFR-SEC-01), otherwise the environment's.
        names = (
            [f"aera/{env_name}/{name}" for name in parameters]
            if parameters is not None
            else [f"aera/{env_name}/*"]
        )
        if names:
            self.function.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["ssm:GetParameter"],
                    resources=[
                        stack.format_arn(service="ssm", resource="parameter", resource_name=name)
                        for name in names
                    ],
                )
            )
        if secrets:
            self.function.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["secretsmanager:GetSecretValue"],
                    resources=[
                        stack.format_arn(
                            service="secretsmanager",
                            resource="secret",
                            resource_name=f"/aera/{env_name}/{name}-*",
                            arn_format=ArnFormat.COLON_RESOURCE_NAME,
                        )
                        for name in secrets
                    ],
                )
            )
