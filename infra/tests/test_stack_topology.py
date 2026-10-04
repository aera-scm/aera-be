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
        # The console distribution lives only in the web stack (SRD 6.19).
        if name != "web":
            template.resource_count_is("AWS::CloudFront::Distribution", 0)
        template.resource_count_is("AWS::KinesisFirehose::DeliveryStream", 0)
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


def test_srd_6_20_web_bucket_name_carries_account_and_region() -> None:
    app = build_app(DataSettings(env_name="dev", owner="synthetic-owner"))
    template = assertions.Template.from_stack(Stack.of(app.node.find_child("aera-dev-web")))
    [bucket] = template.find_resources("AWS::S3::Bucket").values()
    parts = bucket["Properties"]["BucketName"]["Fn::Join"][1]
    assert parts[0] == "aera-dev-web-"
    assert {"Ref": "AWS::AccountId"} in parts
    assert {"Ref": "AWS::Region"} in parts or "us-east-1" in "".join(
        part for part in parts if isinstance(part, str)
    )


def test_nfr_sec_05_private_web_bucket() -> None:
    app = build_app(DataSettings(env_name="dev", owner="synthetic-owner"))
    template = assertions.Template.from_stack(Stack.of(app.node.find_child("aera-dev-web")))
    template.resource_count_is("AWS::S3::Bucket", 1)
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
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
    # Only the console distribution may read (Origin Access Control); everything else denied.
    allowed = [statement for statement in statements if statement["Effect"] == "Allow"]
    assert [s["Principal"] for s in allowed] == [{"Service": "cloudfront.amazonaws.com"}]
    assert all(s["Action"] == "s3:GetObject" for s in allowed)
    assert any(
        statement.get("Condition", {}).get("NumericLessThan", {}).get("s3:TlsVersion") == 1.2
        for statement in statements
    )


def test_srd_6_19_console_is_served_by_cloudfront_with_origin_access_control() -> None:
    app = build_app(DataSettings(env_name="dev", owner="synthetic-owner"))
    template = assertions.Template.from_stack(Stack.of(app.node.find_child("aera-dev-web")))
    template.resource_count_is("AWS::CloudFront::Distribution", 1)
    template.resource_count_is("AWS::CloudFront::OriginAccessControl", 1)
    [distribution] = template.find_resources("AWS::CloudFront::Distribution").values()
    config = distribution["Properties"]["DistributionConfig"]
    assert config["DefaultRootObject"] == "index.html"
    assert config["DefaultCacheBehavior"]["ViewerProtocolPolicy"] == "redirect-to-https"
    # Single-page application: deep links such as /cases/:id/:stage load the console.
    assert {
        (r["ErrorCode"], r["ResponsePagePath"], r["ResponseCode"])
        for r in config["CustomErrorResponses"]
    } == {
        (403, "/index.html", 200),
        (404, "/index.html", 200),
    }
    # The bucket stays private: only this distribution may read it.
    policies = template.find_resources("AWS::S3::BucketPolicy")
    statements = next(iter(policies.values()))["Properties"]["PolicyDocument"]["Statement"]
    readers = [s for s in statements if s["Effect"] == "Allow"]
    assert len(readers) == 1 and readers[0]["Principal"] == {"Service": "cloudfront.amazonaws.com"}


def test_srd_6_19_console_security_headers() -> None:
    app = build_app(DataSettings(env_name="dev", owner="synthetic-owner"))
    template = assertions.Template.from_stack(Stack.of(app.node.find_child("aera-dev-web")))
    [policy] = template.find_resources("AWS::CloudFront::ResponseHeadersPolicy").values()
    headers = policy["Properties"]["ResponseHeadersPolicyConfig"]["SecurityHeadersConfig"]
    assert headers["StrictTransportSecurity"]["AccessControlMaxAgeSec"] >= 31536000
    csp = headers["ContentSecurityPolicy"]["ContentSecurityPolicy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "connect-src 'self' https://*.execute-api.us-east-1.amazonaws.com" in csp
    assert "wss://*.execute-api.us-east-1.amazonaws.com" in csp
    assert "https://*.auth.us-east-1.amazoncognito.com" in csp
    assert "script-src 'self'" in csp and "'unsafe-eval'" not in csp
    # The component library ships its icon and text fonts as data: URIs.
    assert "font-src 'self' data: https://fonts.gstatic.com" in csp
    assert headers["FrameOptions"]["FrameOption"] == "DENY"
    assert headers["ContentTypeOptions"]["Override"] is True


def test_srd_6_19_console_cache_respects_shell_no_cache() -> None:
    app = build_app(DataSettings(env_name="dev", owner="synthetic-owner"))
    template = assertions.Template.from_stack(Stack.of(app.node.find_child("aera-dev-web")))
    [policy] = template.find_resources("AWS::CloudFront::CachePolicy").values()
    config = policy["Properties"]["CachePolicyConfig"]
    assert config["MinTTL"] == 0
    assert config["DefaultTTL"] == 0
    assert config["MaxTTL"] == 31536000
