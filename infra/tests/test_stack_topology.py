"""SRD 6.16 stack graph, bucket protection and M0 scope limits."""

from aws_cdk import Stack, assertions

from infra.app import DataSettings, build_app


def test_nfr_mnt_02_all_stacks_and_dependencies() -> None:
    app = build_app(DataSettings(env_name="dev", owner="synthetic-owner"))
    assembly = app.synth()
    stacks = {
        child.stack_name.removeprefix("aera-dev-"): child
        for child in app.node.children
        if isinstance(child, Stack)
    }
    expected = {
        "data": set(),
        "identity": {"data"},
        "edge": {"data", "identity"},
        "gate": {"data"},
        "reasoning": {"data", "gate"},
        "control": {"data", "reasoning"},
        "interop": {"edge", "identity"},
        "web": {"edge", "identity"},
        "observability": {
            "data",
            "identity",
            "edge",
            "gate",
            "reasoning",
            "control",
            "interop",
            "web",
        },
    }
    assert set(stacks) == set(expected)
    for name, stack in stacks.items():
        assert stack.region == "us-east-1"
        assert {dep.stack_name.removeprefix("aera-dev-") for dep in stack.dependencies} == expected[
            name
        ]
        assert stack.tags.tag_values() == {
            "project": "aera",
            "env": "dev",
            "component": name,
            "owner": "synthetic-owner",
        }
        template = assertions.Template.from_stack(stack)
        # Runtimes live in reasoning and interop; Lambdas stay in their service stacks.
        if name not in {"gate", "edge", "reasoning", "control"}:
            template.resource_count_is("AWS::Lambda::Function", 0)
        if name != "edge":
            template.resource_count_is("AWS::ApiGateway::RestApi", 0)
        if name not in {"reasoning", "interop"}:
            template.resource_count_is("AWS::BedrockAgentCore::Runtime", 0)
        if name != "control":
            template.resource_count_is("AWS::StepFunctions::StateMachine", 0)
        for resource in (
            "AWS::CloudFront::Distribution",
            "AWS::KinesisFirehose::DeliveryStream",
        ):
            template.resource_count_is(resource, 0)
    assembly = app.synth()
    assert len(assembly.stacks) == 9
    for artifact in assembly.stacks:
        assert artifact.template.get("Resources")
        if artifact.stack_name.removeprefix("aera-dev-") not in {
            "data",
            "identity",
            "web",
            "gate",
            "edge",
            "reasoning",
            "control",
            "interop",
        }:
            resources = list(artifact.template["Resources"].values())
            assert len(resources) == 1
            assert resources[0]["Type"] == "AWS::CloudFormation::WaitConditionHandle"
            assert "Ref" not in str(artifact.template["Outputs"])


def test_nfr_sec_05_private_web_bucket() -> None:
    app = build_app(DataSettings(env_name="dev", owner="synthetic-owner"))
    template = assertions.Template.from_stack(Stack.of(app.node.find_child("aera-dev-web")))
    template.resource_count_is("AWS::S3::Bucket", 1)
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "BucketName": "aera-dev-web",
            "BucketEncryption": {
                "ServerSideEncryptionConfiguration": [
                    {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}},
                ]
            },
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            },
        },
    )
    policies = template.find_resources("AWS::S3::BucketPolicy")
    statements = next(iter(policies.values()))["Properties"]["PolicyDocument"]["Statement"]
    assert all(statement["Effect"] == "Deny" for statement in statements)
    assert any(
        statement.get("Condition", {}).get("NumericLessThan", {}).get("s3:TlsVersion") == 1.2
        for statement in statements
    )
