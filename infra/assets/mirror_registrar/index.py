"""Custom resource: register the ECS-hosted SAP Mirror with AERA (SRD 6.16, IR-02).

Reads the Mirror's OAuth client from the Cognito user pool and stores it, with the token
URL, in the Mirror secret; writes the Mirror URL to SAP_READ_BASE and SAP_WRITE_BASE. The
client secret goes from Cognito to Secrets Manager inside this function and nowhere else:
it is never a template property, an output or a log line (NFR-SEC-03).
"""

import json
import urllib.request
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import boto3

PHYSICAL_ID = "aera-mirror-registration"
Send = Callable[[str, dict[str, Any]], None]


def _https(url: str, *, origin_only: bool) -> str:
    parts = urlsplit(url)
    plain = parts.path in ("", "/") and not parts.query and not parts.fragment
    if parts.scheme != "https" or not parts.hostname or (origin_only and not plain):
        raise ValueError("not an https URL")
    return url.rstrip("/") if origin_only else url


def register(properties: dict[str, Any]) -> None:
    mirror_url = _https(str(properties["MirrorUrl"]), origin_only=True)
    token_url = _https(str(properties["TokenUrl"]), origin_only=False)
    client = boto3.client("cognito-idp").describe_user_pool_client(
        UserPoolId=properties["UserPoolId"], ClientId=properties["ClientId"]
    )["UserPoolClient"]
    value = {
        "clientId": client["ClientId"],
        "clientSecret": client["ClientSecret"],
        "tokenUrl": token_url,
    }
    boto3.client("secretsmanager").put_secret_value(
        SecretId=properties["SecretId"], SecretString=json.dumps(value, sort_keys=True)
    )
    ssm = boto3.client("ssm")
    for name in properties["Parameters"]:
        ssm.put_parameter(Name=name, Value=mirror_url, Type="String", Overwrite=True)


def _send(url: str, body: dict[str, Any]) -> None:
    request = urllib.request.Request(  # noqa: S310 - the pre-signed CloudFormation URL
        url, data=json.dumps(body).encode(), method="PUT", headers={"Content-Type": ""}
    )
    with urllib.request.urlopen(request, timeout=30):  # noqa: S310
        pass


def handler(event: dict[str, Any], context: Any, send: Send = _send) -> None:
    status, reason = "SUCCESS", "Mirror registered"
    try:
        if event["RequestType"] in ("Create", "Update"):
            register(event["ResourceProperties"])
        else:
            reason = "Mirror registration left in place"
    except Exception as error:
        # The error type only: messages of AWS errors may quote request values.
        status, reason = "FAILED", f"Mirror registration failed: {type(error).__name__}"
    send(
        event["ResponseURL"],
        {
            "Status": status,
            "Reason": reason,
            "PhysicalResourceId": PHYSICAL_ID,
            "StackId": event["StackId"],
            "RequestId": event["RequestId"],
            "LogicalResourceId": event["LogicalResourceId"],
        },
    )
