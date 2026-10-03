"""Data stack: tables, buckets, key, event bus, parameters, secret containers.

SRD 6.16 (`aera-{env}-data`), 6.20 (data stores) and 6.23 (SSM configuration);
NFR-SEC-03, NFR-SEC-05, NFR-CMP-02. Every store is encrypted with the project
KMS key and retained on stack deletion. Secret containers are created empty;
values are placed at runtime by ``scripts/provision_secrets.py``.
"""

from dataclasses import dataclass
from typing import Any

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from infra.config_defaults import GATE_PARAMETER_KEYS, SSM_PARAMETER_KEYS

# Explicit marker for a deployment value that does not exist yet. Never a
# plausible id or endpoint, so a service reading it fails instead of guessing.
UNSET = "UNSET"
SAP_SANDBOX_BASE_URL = "https://sandbox.api.sap.com/s4hanacloud"  # IR-01

TTL_ATTRIBUTE = "ttl"
RAW_RETENTION = Duration.days(90)
AUDIT_RETENTION = Duration.days(90)

_S = dynamodb.AttributeType.STRING
_N = dynamodb.AttributeType.NUMBER


@dataclass(frozen=True)
class Index:
    name: str
    partition: tuple[str, dynamodb.AttributeType]
    sort: tuple[str, dynamodb.AttributeType] | None = None


@dataclass(frozen=True)
class TableSpec:
    name: str
    sort_key: bool
    indexes: tuple[Index, ...] = ()
    ttl: bool = False
    stream: dynamodb.StreamViewType | None = None


# SRD 6.20. Item layouts use generic PK/SK attributes (e.g. PK `CASE#{caseId}`).
TABLES: tuple[TableSpec, ...] = (
    TableSpec(
        "cases",
        sort_key=True,
        indexes=(
            Index("GSI1", ("status", _S), ("priorityScore", _N)),
            Index("GSI2", ("approverId", _S), ("routedAt", _S)),
        ),
        stream=dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
    ),
    TableSpec(
        "signals",
        sort_key=True,
        indexes=(
            Index("GSI1", ("caseId", _S), ("receivedAt", _S)),
            Index("GSI2", ("poNumber", _S), ("receivedAt", _S)),
        ),
    ),
    TableSpec("trace", sort_key=True, ttl=True, stream=dynamodb.StreamViewType.NEW_IMAGE),
    TableSpec("ledger", sort_key=True),
    TableSpec("idempotency", sort_key=False, ttl=True),
    TableSpec("audit", sort_key=True, stream=dynamodb.StreamViewType.NEW_IMAGE),
    TableSpec("dialogue", sort_key=True, indexes=(Index("GSI1", ("referenceToken", _S)),)),
    TableSpec("analytics", sort_key=True),
    TableSpec("config", sort_key=False),
    TableSpec("connections", sort_key=False, indexes=(Index("GSI1", ("caseId", _S)),), ttl=True),
)


class DataStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        env_name: str,
        model_ids: dict[str, str] | None = None,
        existing_secrets: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.key = kms.Key(
            self,
            "ProjectKey",
            alias=f"alias/aera-{env_name}",
            description=f"AERA {env_name} project key for tables, buckets and secrets",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        self.tables = {spec.name: self._table(env_name, spec) for spec in TABLES}
        self.buckets = {
            "raw": self._bucket(
                env_name,
                "raw",
                lifecycle_rules=[s3.LifecycleRule(id="expire-raw", expiration=RAW_RETENTION)],
            ),
            "artefacts": self._bucket(env_name, "artefacts"),
            "audit": self._bucket(
                env_name,
                "audit",
                object_lock_enabled=True,
                object_lock_default_retention=s3.ObjectLockRetention.compliance(AUDIT_RETENTION),
                versioned=True,
            ),
        }
        self.bus = events.EventBus(self, "Bus", event_bus_name=f"aera-{env_name}")

        # SES receipt rule (edge stack) writes supplier mail under ses/; objects created in the
        # raw bucket are announced on the default bus, which triggers ses-inbound (IR-06).
        raw = self.buckets["raw"]
        # The CloudFormation property, not the L2 flag: the flag adds a custom-resource Lambda
        # and this stack holds no compute.
        raw_cfn = raw.node.default_child
        assert isinstance(raw_cfn, s3.CfnBucket)
        raw_cfn.notification_configuration = s3.CfnBucket.NotificationConfigurationProperty(
            event_bridge_configuration=s3.CfnBucket.EventBridgeConfigurationProperty(
                event_bridge_enabled=True
            )
        )
        ses = iam.ServicePrincipal("ses.amazonaws.com")
        from_this_account = {"StringEquals": {"aws:SourceAccount": self.account}}
        raw.add_to_resource_policy(
            iam.PolicyStatement(
                principals=[ses],
                actions=["s3:PutObject"],
                resources=[raw.arn_for_objects("ses/*")],
                conditions=from_this_account,
            )
        )
        self.key.add_to_resource_policy(
            iam.PolicyStatement(
                principals=[ses],
                actions=["kms:GenerateDataKey*", "kms:Encrypt"],
                resources=["*"],
                conditions=from_this_account,
            )
        )

        # Guardrail id and version come from the gate stack, which creates the Guardrail.
        values = {key: UNSET for key in SSM_PARAMETER_KEYS if key not in GATE_PARAMETER_KEYS}
        values["SAP_SANDBOX_BASE"] = SAP_SANDBOX_BASE_URL
        values.update(model_ids or {})
        for key, value in values.items():
            ssm.StringParameter(
                self,
                f"Param{key}",
                parameter_name=f"/aera/{env_name}/{key}",
                string_value=value,
                description=f"SRD 6.23 {key}",
            )

        existing = existing_secrets or {}
        for path, component, description in (
            ("sap", "sandbox-api-key", "SAP Business Accelerator Hub sandbox API key (IR-01)"),
            ("sap", "mirror-oauth-client", "SAP Mirror OAuth client credentials (IR-02)"),
            ("channels", "whatsapp", "WhatsApp app secret, verify and access token (IR-07)"),
            ("channels", "carrier-webhook", "Carrier webhook HMAC keys by carrier id (IR-08)"),
            ("channels", "email-standins", "Verified stand-in addresses and sender (FR-COM-04)"),
        ):
            if component in existing:
                continue
            # L1 on purpose: the L2 Secret generates a random value when none is given.
            secret = secretsmanager.CfnSecret(
                self,
                f"Secret-{component}",
                name=f"/aera/{env_name}/{path}/{component}",
                description=f"{description}; value placed at runtime, never in code",
                kms_key_id=self.key.key_arn,
            )
            secret.apply_removal_policy(RemovalPolicy.RETAIN)

    def _table(self, env_name: str, spec: TableSpec) -> dynamodb.Table:
        table = dynamodb.Table(
            self,
            f"Table-{spec.name}",
            table_name=f"aera-{env_name}-{spec.name}",
            partition_key=dynamodb.Attribute(name="PK", type=_S),
            sort_key=dynamodb.Attribute(name="SK", type=_S) if spec.sort_key else None,
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=self.key,
            time_to_live_attribute=TTL_ATTRIBUTE if spec.ttl else None,
            stream=spec.stream,
            deletion_protection=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        for index in spec.indexes:
            table.add_global_secondary_index(
                index_name=index.name,
                partition_key=dynamodb.Attribute(name=index.partition[0], type=index.partition[1]),
                sort_key=(
                    dynamodb.Attribute(name=index.sort[0], type=index.sort[1])
                    if index.sort
                    else None
                ),
                projection_type=dynamodb.ProjectionType.ALL,
            )
        return table

    def _bucket(self, env_name: str, component: str, **kwargs: Any) -> s3.Bucket:
        # S3 names are global: account and region keep them unique on a clean account.
        return s3.Bucket(
            self,
            f"Bucket-{component}",
            bucket_name=f"aera-{env_name}-{component}-{self.account}-{self.region}",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.key,
            bucket_key_enabled=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            minimum_tls_version=1.2,
            removal_policy=RemovalPolicy.RETAIN,
            **kwargs,
        )
