import os
import uuid

from botocore.exceptions import ClientError

from common import (
    ATTACHMENTS_BUCKET,
    ApiError,
    get_owned_item,
    now_iso,
    parse_body,
    path_parameter,
    public_item,
    query_prefix,
    response,
    s3,
    table,
    user_id,
    user_pk,
)


ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024


def _require_bucket():
    if not ATTACHMENTS_BUCKET:
        raise ApiError(500, "ATTACHMENTS_BUCKET is not configured")


def _attachment_key(event, task_id, attachment_id, file_name):
    extension = os.path.splitext(file_name)[1].lower()[:10]
    return f"users/{user_id(event)}/tasks/{task_id}/{attachment_id}{extension}"


def create_upload_url(event):
    _require_bucket()
    task_id = path_parameter(event, "taskId")
    get_owned_item(event, f"TASK#{task_id}")
    body = parse_body(event)

    file_name = body.get("fileName")
    content_type = body.get("contentType")
    size = body.get("size")
    if not isinstance(file_name, str) or not file_name.strip() or len(file_name) > 255:
        raise ApiError(400, "'fileName' must be a non-empty string")
    if content_type not in ALLOWED_IMAGE_TYPES:
        raise ApiError(400, "Only JPEG, PNG, WebP, and GIF images are supported")
    if not isinstance(size, int) or size < 1 or size > MAX_IMAGE_BYTES:
        raise ApiError(400, "Image size must be between 1 byte and 10 MB")

    attachment_id = str(uuid.uuid4())
    object_key = _attachment_key(event, task_id, attachment_id, file_name)
    item = {
        "PK": user_pk(event),
        "SK": f"ATTACHMENT#{task_id}#{attachment_id}",
        "entityType": "ATTACHMENT",
        "taskId": task_id,
        "attachmentId": attachment_id,
        "fileName": file_name,
        "contentType": content_type,
        "expectedSize": size,
        "s3Key": object_key,
        "uploadState": "PENDING",
        "createdAt": now_iso(),
    }
    table.put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")

    upload_url = s3.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": ATTACHMENTS_BUCKET,
            "Key": object_key,
            "ContentType": content_type,
        },
        ExpiresIn=900,
    )
    return response(
        201,
        {
            "attachment": public_item(item),
            "uploadUrl": upload_url,
            "expiresIn": 900,
            "requiredHeaders": {"Content-Type": content_type},
        },
    )


def confirm_upload(event):
    _require_bucket()
    task_id = path_parameter(event, "taskId")
    attachment_id = path_parameter(event, "attachmentId")
    item = get_owned_item(event, f"ATTACHMENT#{task_id}#{attachment_id}")

    try:
        uploaded = s3.head_object(Bucket=ATTACHMENTS_BUCKET, Key=item["s3Key"])
    except ClientError as exc:
        if exc.response["Error"]["Code"] in {"404", "NoSuchKey", "NotFound"}:
            raise ApiError(409, "The image has not been uploaded yet") from exc
        raise

    actual_size = uploaded.get("ContentLength", 0)
    if actual_size > MAX_IMAGE_BYTES:
        s3.delete_object(Bucket=ATTACHMENTS_BUCKET, Key=item["s3Key"])
        raise ApiError(400, "Uploaded image exceeds the 10 MB limit")

    item["size"] = actual_size
    item["uploadState"] = "READY"
    item["confirmedAt"] = now_iso()
    table.put_item(Item=item, ConditionExpression="attribute_exists(PK)")
    return response(200, public_item(item))


def list_attachments(event):
    _require_bucket()
    task_id = path_parameter(event, "taskId")
    get_owned_item(event, f"TASK#{task_id}")
    items = query_prefix(user_pk(event), f"ATTACHMENT#{task_id}#")
    result = []
    for item in items:
        public = public_item(item)
        if item.get("uploadState") == "READY":
            public["downloadUrl"] = s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": ATTACHMENTS_BUCKET, "Key": item["s3Key"]},
                ExpiresIn=900,
            )
        result.append(public)
    return response(200, {"items": result})


def delete_attachment(event):
    _require_bucket()
    task_id = path_parameter(event, "taskId")
    attachment_id = path_parameter(event, "attachmentId")
    item = get_owned_item(event, f"ATTACHMENT#{task_id}#{attachment_id}")
    s3.delete_object(Bucket=ATTACHMENTS_BUCKET, Key=item["s3Key"])
    table.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
    return response(204)


def delete_all_for_task(event, task_id):
    items = query_prefix(user_pk(event), f"ATTACHMENT#{task_id}#")
    with table.batch_writer() as batch:
        for item in items:
            if ATTACHMENTS_BUCKET and item.get("s3Key"):
                s3.delete_object(Bucket=ATTACHMENTS_BUCKET, Key=item["s3Key"])
            batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
