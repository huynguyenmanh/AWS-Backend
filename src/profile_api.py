import os
import uuid

from botocore.exceptions import ClientError

from common import (
    ATTACHMENTS_BUCKET,
    USER_POOL_ID,
    ApiError,
    bearer_token,
    claims,
    cognito,
    now_iso,
    parse_body,
    public_item,
    query_all,
    response,
    s3,
    table,
    user_id,
    user_pk,
)


PROFILE_FIELDS = {"displayName", "preferredView", "email"}
ALLOWED_AVATAR_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_AVATAR_BYTES = 5 * 1024 * 1024


def _cognito_attributes(event):
    try:
        result = cognito.get_user(AccessToken=bearer_token(event))
    except ClientError:
        return {}
    return {entry["Name"]: entry["Value"] for entry in result.get("UserAttributes", [])}


def get_profile(event):
    result = table.get_item(Key={"PK": user_pk(event), "SK": "PROFILE"})
    item = result.get("Item")
    if not item:
        timestamp = now_iso()
        item = {
            "PK": user_pk(event),
            "SK": "PROFILE",
            "entityType": "PROFILE",
            "displayName": "",
            "avatarKey": None,
            "preferredView": "LIST",
            "createdAt": timestamp,
            "updatedAt": timestamp,
        }
        try:
            table.put_item(
                Item=item,
                ConditionExpression=(
                    "attribute_not_exists(PK) AND attribute_not_exists(SK)"
                ),
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            # A concurrent first request created the profile. Return that item
            # instead of overwriting it with defaults.
            concurrent = table.get_item(
                Key={"PK": user_pk(event), "SK": "PROFILE"}
            ).get("Item")
            if concurrent:
                item = concurrent
    identity = _cognito_attributes(event)
    identity.setdefault("username", claims(event).get("username"))
    result_profile = public_item(item)
    if item.get("avatarKey") and ATTACHMENTS_BUCKET:
        result_profile["avatarUrl"] = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": ATTACHMENTS_BUCKET, "Key": item["avatarKey"]},
            ExpiresIn=900,
        )
    return response(200, {"profile": result_profile, "identity": identity})


def update_profile(event):
    body = parse_body(event)
    unknown = set(body) - PROFILE_FIELDS
    if unknown:
        raise ApiError(400, "Unsupported profile fields", sorted(unknown))

    result = table.get_item(Key={"PK": user_pk(event), "SK": "PROFILE"})
    timestamp = now_iso()
    item = result.get("Item") or {
        "PK": user_pk(event),
        "SK": "PROFILE",
        "entityType": "PROFILE",
        "createdAt": timestamp,
    }

    if "displayName" in body:
        value = body["displayName"]
        if not isinstance(value, str) or len(value.strip()) > 100:
            raise ApiError(400, "'displayName' must be a string up to 100 characters")
        item["displayName"] = value.strip()

    if "preferredView" in body:
        value = body["preferredView"]
        if value not in {"LIST", "KANBAN", "TIMELINE", "CALENDAR"}:
            raise ApiError(400, "Unsupported preferred view")
        item["preferredView"] = value

    email_update_pending = False
    if "email" in body:
        email = body["email"]
        if not isinstance(email, str) or "@" not in email:
            raise ApiError(400, "'email' must be a valid email address")
        cognito.update_user_attributes(
            AccessToken=bearer_token(event),
            UserAttributes=[{"Name": "email", "Value": email.strip()}],
        )
        email_update_pending = True

    item["updatedAt"] = timestamp
    table.put_item(Item=item)
    return response(
        200,
        {
            "profile": public_item(item),
            "emailVerificationRequired": email_update_pending,
        },
    )


def create_avatar_upload_url(event):
    if not ATTACHMENTS_BUCKET:
        raise ApiError(500, "ATTACHMENTS_BUCKET is not configured")
    body = parse_body(event)
    file_name = body.get("fileName")
    content_type = body.get("contentType")
    size = body.get("size")
    if not isinstance(file_name, str) or not file_name.strip():
        raise ApiError(400, "'fileName' must be a non-empty string")
    if content_type not in ALLOWED_AVATAR_TYPES:
        raise ApiError(400, "Avatar must be a JPEG, PNG, or WebP image")
    if not isinstance(size, int) or size < 1 or size > MAX_AVATAR_BYTES:
        raise ApiError(400, "Avatar size must be between 1 byte and 5 MB")

    extension = os.path.splitext(file_name)[1].lower()[:10]
    avatar_key = f"users/{user_id(event)}/profile/{uuid.uuid4()}{extension}"
    upload_url = s3.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": ATTACHMENTS_BUCKET,
            "Key": avatar_key,
            "ContentType": content_type,
        },
        ExpiresIn=900,
    )
    return response(
        201,
        {
            "avatarKey": avatar_key,
            "uploadUrl": upload_url,
            "expiresIn": 900,
            "requiredHeaders": {"Content-Type": content_type},
        },
    )


def confirm_avatar(event):
    if not ATTACHMENTS_BUCKET:
        raise ApiError(500, "ATTACHMENTS_BUCKET is not configured")
    body = parse_body(event)
    avatar_key = body.get("avatarKey")
    expected_prefix = f"users/{user_id(event)}/profile/"
    if not isinstance(avatar_key, str) or not avatar_key.startswith(expected_prefix):
        raise ApiError(400, "Invalid avatar key")
    try:
        uploaded = s3.head_object(Bucket=ATTACHMENTS_BUCKET, Key=avatar_key)
    except ClientError as exc:
        if exc.response["Error"]["Code"] in {"404", "NoSuchKey", "NotFound"}:
            raise ApiError(409, "The avatar has not been uploaded yet") from exc
        raise
    if uploaded.get("ContentLength", 0) > MAX_AVATAR_BYTES:
        s3.delete_object(Bucket=ATTACHMENTS_BUCKET, Key=avatar_key)
        raise ApiError(400, "Uploaded avatar exceeds the 5 MB limit")

    result = table.get_item(Key={"PK": user_pk(event), "SK": "PROFILE"})
    old_item = result.get("Item")
    timestamp = now_iso()
    item = old_item or {
        "PK": user_pk(event),
        "SK": "PROFILE",
        "entityType": "PROFILE",
        "createdAt": timestamp,
    }
    previous_key = item.get("avatarKey")
    item["avatarKey"] = avatar_key
    item["updatedAt"] = timestamp
    table.put_item(Item=item)
    if previous_key and previous_key != avatar_key:
        s3.delete_object(Bucket=ATTACHMENTS_BUCKET, Key=previous_key)
    return response(200, {"profile": public_item(item)})


def delete_avatar(event):
    result = table.get_item(Key={"PK": user_pk(event), "SK": "PROFILE"})
    item = result.get("Item")
    if not item or not item.get("avatarKey"):
        return response(204)
    if ATTACHMENTS_BUCKET:
        s3.delete_object(Bucket=ATTACHMENTS_BUCKET, Key=item["avatarKey"])
    item["avatarKey"] = None
    item["updatedAt"] = now_iso()
    table.put_item(Item=item)
    return response(204)


def _delete_user_s3_objects(owner_id):
    if not ATTACHMENTS_BUCKET:
        return
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(
        Bucket=ATTACHMENTS_BUCKET, Prefix=f"users/{owner_id}/"
    ):
        objects = [{"Key": item["Key"]} for item in page.get("Contents", [])]
        if objects:
            s3.delete_objects(
                Bucket=ATTACHMENTS_BUCKET, Delete={"Objects": objects, "Quiet": True}
            )


def delete_account(event):
    if not USER_POOL_ID:
        raise ApiError(500, "USER_POOL_ID is not configured")

    identity = claims(event)
    username = identity.get("username")
    if not username:
        raise ApiError(400, "Cognito username claim is missing")

    #signing out before disabling
    cognito.admin_user_global_sign_out(UserPoolId=USER_POOL_ID,Username=username)
    # Disabling first prevents additional writes during deletion.
    cognito.admin_disable_user(UserPoolId=USER_POOL_ID, Username=username)

    owner_id = user_id(event)
    items = query_all(user_pk(event))
    with table.batch_writer() as batch:
        for item in items:
            batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})

    _delete_user_s3_objects(owner_id)
    cognito.admin_delete_user(UserPoolId=USER_POOL_ID, Username=username)
    return response(204)
