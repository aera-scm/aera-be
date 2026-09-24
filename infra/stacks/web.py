"""Private web storage; distribution and bundle deferred (SRD 6.20)."""

from typing import Any

from aws_cdk import RemovalPolicy, Stack
from aws_cdk import aws_s3 as s3
from constructs import Construct

from infra.environments import require_deployable_environment


class WebStack(Stack):
    def __init__(
        self, scope: Construct, construct_id: str, *, env_name: str, **kwargs: Any
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        require_deployable_environment(env_name)
        s3.Bucket(
            self,
            "Web",
            bucket_name=f"aera-{env_name}-web",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            enforce_ssl=True,
            minimum_tls_version=1.2,
            removal_policy=RemovalPolicy.RETAIN,
        )
