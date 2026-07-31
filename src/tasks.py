import uuid

from botocore.exceptions import ClientError

from common import (
    ApiError,
    get_owned_item,
    now_iso,
    parse_body,
    path_parameter,
    public_item,
    query_parameters,
    query_prefix,
    require_string,
    response,
    table,
    user_id,
    user_pk,
    validate_date,
)


TASK_FIELDS = {
    "title",
    "description",
    "recordState",
    "statusId",
    "categoryId",
    "tagIds",
    "date",
    "startDate",
}
RECORD_STATES = {"DRAFT", "ACTIVE"}


def _validate_references(owner_id, values):
    references = []
    if values.get("statusId"):
        references.append(("statusId", "STATUS", values["statusId"]))
    if values.get("categoryId"):
        references.append(("categoryId", "CATEGORY", values["categoryId"]))
    references.extend(("tagIds", "TAG", tag_id) for tag_id in values.get("tagIds", []))

    for field, prefix, reference_id in references:
        result = table.get_item(
            Key={"PK": f"USER#{owner_id}", "SK": f"{prefix}#{reference_id}"}
        )
        if not result.get("Item"):
            raise ApiError(400, f"'{field}' contains an unknown ID: {reference_id}")


def _validate_task_values(body, partial=False):
    result = {}

    if "title" in body:
        result["title"] = require_string(
            body, "title", allow_empty=True, maximum=200
        )
        result["titleNormalized"] = result["title"].casefold()
    elif not partial:
        result["title"] = ""
        result["titleNormalized"] = ""

    if "description" in body:
        description = body["description"]
        if not isinstance(description, str):
            raise ApiError(400, "'description' must be a string")
        if len(description) > 100_000:
            raise ApiError(400, "'description' cannot exceed 100000 characters")
        result["description"] = description
    elif not partial:
        result["description"] = ""

    if "recordState" in body:
        record_state = body["recordState"]
        if record_state not in RECORD_STATES:
            raise ApiError(400, "'recordState' must be DRAFT or ACTIVE")
        result["recordState"] = record_state
    elif not partial:
        result["recordState"] = "ACTIVE"

    for field in ("statusId", "categoryId"):
        if field in body:
            value = body[field]
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ApiError(400, f"'{field}' must be a non-empty string or null")
            result[field] = value.strip() if isinstance(value, str) else None
            if isinstance(result[field], str) and len(result[field]) > 100:
                raise ApiError(400, f"'{field}' cannot exceed 100 characters")
        elif not partial:
            result[field] = None

    if "tagIds" in body:
        tag_ids = body["tagIds"]
        if not isinstance(tag_ids, list) or not all(
            isinstance(tag, str) and tag.strip() for tag in tag_ids
        ):
            raise ApiError(400, "'tagIds' must be an array of non-empty strings")
        result["tagIds"] = list(dict.fromkeys(tag.strip() for tag in tag_ids))
        if len(result["tagIds"]) > 50:
            raise ApiError(400, "A task cannot contain more than 50 tags")
    elif not partial:
        result["tagIds"] = []

    for field in ("date", "startDate"):
        if field in body:
            result[field] = validate_date(body[field], field)
        elif not partial:
            result[field] = None

    return result


def _apply_index_attributes(item, owner_id):
    for key in (
        "datePK",
        "dateSK",
        "statusPK",
        "statusSK",
        "categoryPK",
        "categorySK",
    ):
        item.pop(key, None)

    if item.get("recordState") != "ACTIVE":
        return item

    task_id = item["taskId"]
    owner = f"USER#{owner_id}"

    if item.get("date"):
        item["datePK"] = owner
        item["dateSK"] = f"DATE#{item['date']}#TASK#{task_id}"

    if item.get("statusId"):
        item["statusPK"] = f"{owner}#STATUS#{item['statusId']}"
        item["statusSK"] = f"TASK#{task_id}"

    if item.get("categoryId"):
        item["categoryPK"] = f"{owner}#CATEGORY#{item['categoryId']}"
        item["categorySK"] = f"TASK#{task_id}"

    return item


def build_task_item(owner_id, body, task_id=None):
    values = _validate_task_values(body)
    if values["recordState"] == "ACTIVE" and not values["title"]:
        raise ApiError(400, "An active task must have a title")
    _validate_references(owner_id, values)

    task_id = task_id or str(uuid.uuid4())
    timestamp = now_iso()
    item = {
        "PK": f"USER#{owner_id}",
        "SK": f"TASK#{task_id}",
        "entityType": "TASK",
        "taskId": task_id,
        **values,
        "createdAt": timestamp,
        "updatedAt": timestamp,
        "version": 1,
    }
    return _apply_index_attributes(item, owner_id)


def create_task(event):
    body = parse_body(event)
    item = build_task_item(user_id(event), body)
    try:
        table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise ApiError(409, "Task already exists") from exc
        raise
    return response(201, public_item(item))


def get_task(event):
    task_id = path_parameter(event, "taskId")
    return response(200, public_item(get_owned_item(event, f"TASK#{task_id}")))


def _matches_query(task, params):
    if params.get("recordState") and task.get("recordState") != params["recordState"]:
        return False
    if params.get("statusId") and task.get("statusId") != params["statusId"]:
        return False
    if params.get("categoryId") and task.get("categoryId") != params["categoryId"]:
        return False
    if params.get("tagId") and params["tagId"] not in task.get("tagIds", []):
        return False

    search = params.get("search", "").strip().casefold()
    if search and search not in task.get("titleNormalized", ""):
        return False

    task_date = task.get("date")
    if params.get("date") and task_date != params["date"]:
        return False
    if params.get("fromDate") and (not task_date or task_date < params["fromDate"]):
        return False
    if params.get("toDate") and (not task_date or task_date > params["toDate"]):
        return False
    if params.get("beforeDate") and (not task_date or task_date >= params["beforeDate"]):
        return False
    return True


def list_task_items(event, include_custom_filter=True):
    params = query_parameters(event)
    for name in ("date", "fromDate", "toDate", "beforeDate"):
        if params.get(name):
            validate_date(params[name], name)

    items = query_prefix(user_pk(event), "TASK#")
    items = [item for item in items if _matches_query(item, params)]

    if include_custom_filter and params.get("filterId"):
        from filters import apply_saved_filter

        items = apply_saved_filter(event, params["filterId"], items)

    sort_by = params.get("sortBy", "updatedAt")
    if sort_by not in {"updatedAt", "createdAt", "date", "title"}:
        raise ApiError(400, "Unsupported sortBy value")
    descending = params.get("order", "desc").lower() != "asc"
    items.sort(key=lambda item: item.get(sort_by) or "", reverse=descending)
    return items


def list_tasks(event):
    items = list_task_items(event)
    return response(
        200,
        {"items": [public_item(item) for item in items], "count": len(items)},
    )


def update_task(event):
    task_id = path_parameter(event, "taskId")
    existing = get_owned_item(event, f"TASK#{task_id}")
    body = parse_body(event)
    unknown = set(body) - TASK_FIELDS - {"expectedVersion"}
    if unknown:
        raise ApiError(400, "Unsupported task fields", sorted(unknown))

    expected_version = body.get("expectedVersion")
    current_version = int(existing.get("version", 1))
    if expected_version is not None and expected_version != current_version:
        raise ApiError(409, "Task was updated by another request")

    changes = _validate_task_values(body, partial=True)
    updated = {**existing, **changes}
    if updated.get("recordState") == "ACTIVE" and not updated.get("title", "").strip():
        raise ApiError(400, "An active task must have a title")
    _validate_references(user_id(event), updated)

    updated["updatedAt"] = now_iso()
    updated["version"] = current_version + 1
    _apply_index_attributes(updated, user_id(event))

    try:
        table.put_item(
            Item=updated,
            ConditionExpression="attribute_exists(PK) AND #version = :version",
            ExpressionAttributeNames={"#version": "version"},
            ExpressionAttributeValues={":version": current_version},
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise ApiError(409, "Task was updated by another request") from exc
        raise
    return response(200, public_item(updated))


def delete_task(event):
    task_id = path_parameter(event, "taskId")
    get_owned_item(event, f"TASK#{task_id}")

    from attachments import delete_all_for_task

    delete_all_for_task(event, task_id)
    table.delete_item(
        Key={"PK": user_pk(event), "SK": f"TASK#{task_id}"},
        ConditionExpression="attribute_exists(PK)",
    )
    return response(204)
