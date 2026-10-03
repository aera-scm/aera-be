"""Deployable stack placeholder with no runtime resources (NFR-MNT-02)."""

from typing import Any

from aws_cdk import CfnOutput, CfnWaitConditionHandle, Stack
from constructs import Construct

from infra.environments import require_deployable_environment


class ShellStack(Stack):
    def __init__(
        self, scope: Construct, construct_id: str, *, env_name: str, **kwargs: Any
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        require_deployable_environment(env_name)
        # CloudFormation requires a non-empty Resources section. No WaitCondition
        # consumes this handle, so deployment never waits for an external signal.
        # Never export the handle's presigned URL.
        CfnWaitConditionHandle(self, "FoundationAnchor")
        CfnOutput(self, "FoundationStatus", value="Shell only; runtime resources deferred")
