"""Console hosting (SRD 6.19, 6.20): private bucket behind CloudFront with Origin Access
Control and security headers. The console build is uploaded by `scripts/deploy_console.py`
from the aera-fe build, so this stack never embeds the frontend."""

from typing import Any

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_s3 as s3
from constructs import Construct

from infra.environments import require_deployable_environment


class WebStack(Stack):
    def __init__(
        self, scope: Construct, construct_id: str, *, env_name: str, **kwargs: Any
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        require_deployable_environment(env_name)
        bucket = s3.Bucket(
            self,
            "Web",
            # S3 names are global; the suffix keeps a clean account deployable (SRD 6.20).
            bucket_name=f"aera-{env_name}-web-{self.account}-{self.region}",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            enforce_ssl=True,
            minimum_tls_version=1.2,
            removal_policy=RemovalPolicy.RETAIN,
        )
        region = self.region
        # The console talks only to the AERA API, its WebSocket and the Cognito Hosted UI.
        csp = "; ".join(
            [
                "default-src 'self'",
                "script-src 'self'",
                "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
                "font-src 'self' https://fonts.gstatic.com",
                "img-src 'self' data:",
                f"connect-src 'self' https://*.execute-api.{region}.amazonaws.com "
                f"wss://*.execute-api.{region}.amazonaws.com "
                f"https://*.auth.{region}.amazoncognito.com "
                f"https://cognito-idp.{region}.amazonaws.com",
                "frame-ancestors 'none'",
                "base-uri 'self'",
                "form-action 'self'",
            ]
        )
        headers = cloudfront.ResponseHeadersPolicy(
            self,
            "SecurityHeaders",
            response_headers_policy_name=f"aera-{env_name}-console-headers",
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                content_security_policy=cloudfront.ResponseHeadersContentSecurityPolicy(
                    content_security_policy=csp, override=True
                ),
                strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                    access_control_max_age=Duration.days(365),
                    include_subdomains=True,
                    override=True,
                ),
                frame_options=cloudfront.ResponseHeadersFrameOptions(
                    frame_option=cloudfront.HeadersFrameOption.DENY, override=True
                ),
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(override=True),
                referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                    referrer_policy=cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN,
                    override=True,
                ),
            ),
        )
        distribution = cloudfront.Distribution(
            self,
            "Console",
            comment=f"aera-{env_name} console",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                response_headers_policy=headers,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
            ),
            # Single-page application: every deep link loads the console shell.
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=status,
                    response_http_status=200,
                    response_page_path="/index.html",
                    ttl=Duration.seconds(0),
                )
                for status in (403, 404)
            ],
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
        )
        CfnOutput(self, "ConsoleUrl", value=f"https://{distribution.distribution_domain_name}")
        CfnOutput(self, "ConsoleDistributionId", value=distribution.distribution_id)
        CfnOutput(self, "ConsoleBucket", value=bucket.bucket_name)
