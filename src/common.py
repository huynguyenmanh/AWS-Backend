import json
import os
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.config import Config
from botocore.exceptions import ClientError


def required_environment_variable(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Required environment variable '{name}' is not configured"
        )
    return value


# Resource identifiers
TABLE_NAME = required_environment_variable("TABLE_NAME")
ATTACHMENTS_BUCKET = required_environment_variable("ATTACHMENTS_BUCKET")
USER_POOL_ID = required_environment_variable("USER_POOL_ID")

# Resource regions
DYNAMODB_REGION = required_environment_variable("DYNAMODB_REGION")
S3_REGION = required_environment_variable("S3_REGION")
COGNITO_REGION = required_environment_variable("COGNITO_REGION")

# CORS configuration
ALLOWED_ORIGIN = required_environment_variable("ALLOWED_ORIGIN")


dynamodb = boto3.resource(
    "dynamodb",
    region_name=DYNAMODB_REGION
)
table = dynamodb.Table(TABLE_NAME)

s3 = boto3.client(
    "s3",
    region_name=S3_REGION,
    config=Config(signature_version="s3v4")
)

cognito = boto3.client(
    "cognito-idp",
    region_name=COGNITO_REGION
)


class ApiError(Exception):
    def __init__(self, status_code, message, details=None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.details = details


def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def json_default(value):
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    if isinstance(value, set):
        return list(value)
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def response(status_code, body=None, headers=None):
    response_headers = {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
        "Access-Control-Allow-Headers": (
            "Content-Type,Authorization,X-Amz-Date,"
            "X-Api-Key,X-Amz-Security-Token"
        ),
        "Access-Control-Allow-Methods": (
            "OPTIONS,GET,POST,PUT,PATCH,DELETE"
        )
    }

    if headers:
        response_headers.update(headers)

    result = {
        "statusCode": status_code,
        "headers": response_headers
    }

    if body is not None:
        result["body"] = (
            body
            if isinstance(body, str)
            else json.dumps(
                body,
                default=json_default,
                ensure_ascii=False
            )
        )

    return result


def error_response(error):
    payload = {"message": error.message}
    if error.details is not None:
        payload["details"] = error.details
    return response(error.status_code, payload)


def parse_body(event, required=True):
    raw_body = event.get("body")
    if not raw_body:
        if required:
            raise ApiError(400, "A JSON request body is required")
        return {}

    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise ApiError(400, "The request body is not valid JSON") from exc

    if not isinstance(body, dict):
        raise ApiError(400, "The request body must be a JSON object")
    return body


def claims(event):
    result = (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("jwt", {})
        .get("claims")
    )
    if not result or not result.get("sub"):
        raise ApiError(401, "Authentication information is missing")
    return result


def user_id(event):
    return claims(event)["sub"]


def user_pk(event_or_user_id):
    value = (
        user_id(event_or_user_id)
        if isinstance(event_or_user_id, dict)
        else event_or_user_id
    )
    return f"USER#{value}"


def bearer_token(event):
    header = event.get("headers", {}).get("authorization", "")
    if not header:
        header = event.get("headers", {}).get("Authorization", "")
    if not header.lower().startswith("bearer "):
        raise ApiError(401, "Bearer access token is missing")
    return header.split(" ", 1)[1].strip()


def path_parameter(event, name):
    value = (event.get("pathParameters") or {}).get(name)
    if not value:
        raise ApiError(400, f"Path parameter '{name}' is required")
    return value


def query_parameters(event):
    return event.get("queryStringParameters") or {}


def query_prefix(pk, prefix):
    items = []
    request = {
        "KeyConditionExpression": Key("PK").eq(pk) & Key("SK").begins_with(prefix)
    }

    while True:
        result = table.query(**request)
        items.extend(result.get("Items", []))
        last_key = result.get("LastEvaluatedKey")
        if not last_key:
            return items
        request["ExclusiveStartKey"] = last_key


def query_all(pk):
    items = []
    request = {"KeyConditionExpression": Key("PK").eq(pk)}
    while True:
        result = table.query(**request)
        items.extend(result.get("Items", []))
        last_key = result.get("LastEvaluatedKey")
        if not last_key:
            return items
        request["ExclusiveStartKey"] = last_key


def get_owned_item(event, sort_key, required=True):
    result = table.get_item(Key={"PK": user_pk(event), "SK": sort_key})
    item = result.get("Item")
    if required and not item:
        raise ApiError(404, "Resource not found")
    return item


def public_item(item):
    if not item:
        return item
    hidden = {
        "PK",
        "SK",
        "datePK",
        "dateSK",
        "statusPK",
        "statusSK",
        "categoryPK",
        "categorySK",
    }
    return {key: value for key, value in item.items() if key not in hidden}


def require_string(body, field, *, allow_empty=False, maximum=200):
    value = body.get(field)
    if not isinstance(value, str):
        raise ApiError(400, f"'{field}' must be a string")
    value = value.strip()
    if not allow_empty and not value:
        raise ApiError(400, f"'{field}' cannot be empty")
    if len(value) > maximum:
        raise ApiError(400, f"'{field}' cannot exceed {maximum} characters")
    return value


def validate_date(value, field):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ApiError(400, f"'{field}' must use YYYY-MM-DD format")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ApiError(400, f"'{field}' must use YYYY-MM-DD format") from exc
    return value


def delete_items(items):
    if not items:
        return
    with table.batch_writer() as batch:
        for item in items:
            batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})

def require_active_account(event):
    identity = claims(event)
    username = identity.get("username")

    if not USER_POOL_ID:
        raise ApiError(500, "USER_POOL_ID is not configured")

    if not username:
        raise ApiError(401, "Username claim is missing")

    try:
        user = cognito.admin_get_user(
            UserPoolId=USER_POOL_ID,
            Username=username
        )
    except ClientError as error:
        error_code = error.response["Error"]["Code"]

        if error_code in {
            "UserNotFoundException",
            "NotAuthorizedException"
        }:
            raise ApiError(
                401,
                "This account no longer exists"
            ) from error

        raise

    if not user.get("Enabled", False):
        raise ApiError(
            401,
            "This account is disabled"
        )
