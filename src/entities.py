import uuid

from common import (
    ApiError,
    get_owned_item,
    now_iso,
    parse_body,
    path_parameter,
    public_item,
    query_prefix,
    require_string,
    response,
    table,
    user_pk,
)


CONFIG = {
    "status": {"prefix": "STATUS", "id": "statusId", "path": "statusId"},
    "category": {"prefix": "CATEGORY", "id": "categoryId", "path": "categoryId"},
    "tag": {"prefix": "TAG", "id": "tagId", "path": "tagId"},
}


def _config(kind):
    if kind not in CONFIG:
        raise ApiError(500, "Invalid entity configuration")
    return CONFIG[kind]


def create_entity(event, kind):
    config = _config(kind)
    body = parse_body(event)
    name = require_string(body, "name", maximum=100)
    entity_id = str(uuid.uuid4())
    timestamp = now_iso()
    item = {
        "PK": user_pk(event),
        "SK": f"{config['prefix']}#{entity_id}",
        "entityType": config["prefix"],
        config["id"]: entity_id,
        "name": name,
        "nameNormalized": name.casefold(),
        "createdAt": timestamp,
        "updatedAt": timestamp,
    }

    if kind in {"status", "category"}:
        color = body.get("color", "#64748B")
        if not isinstance(color, str) or len(color) > 20:
            raise ApiError(400, "'color' must be a valid color string")
        item["color"] = color
    if kind == "status":
        existing = query_prefix(user_pk(event), "STATUS#")
        item["position"] = (len(existing) + 1) * 1000

    table.put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
    return response(201, public_item(item))


def list_entities(event, kind):
    config = _config(kind)
    items = query_prefix(user_pk(event), f"{config['prefix']}#")
    if kind == "status":
        items.sort(key=lambda item: int(item.get("position", 0)))
    else:
        items.sort(key=lambda item: item.get("nameNormalized", ""))
    return response(200, {"items": [public_item(item) for item in items]})


def update_entity(event, kind):
    config = _config(kind)
    entity_id = path_parameter(event, config["path"])
    item = get_owned_item(event, f"{config['prefix']}#{entity_id}")
    body = parse_body(event)

    if "name" in body:
        name = require_string(body, "name", maximum=100)
        item["name"] = name
        item["nameNormalized"] = name.casefold()
    if "color" in body and kind in {"status", "category"}:
        color = body["color"]
        if not isinstance(color, str) or len(color) > 20:
            raise ApiError(400, "'color' must be a valid color string")
        item["color"] = color
    item["updatedAt"] = now_iso()
    table.put_item(Item=item, ConditionExpression="attribute_exists(PK)")
    return response(200, public_item(item))


def delete_entity(event, kind):
    config = _config(kind)
    entity_id = path_parameter(event, config["path"])
    get_owned_item(event, f"{config['prefix']}#{entity_id}")

    tasks = query_prefix(user_pk(event), "TASK#")
    reference_field = config["id"]
    if kind == "tag":
        in_use = any(entity_id in task.get("tagIds", []) for task in tasks)
    else:
        in_use = any(task.get(reference_field) == entity_id for task in tasks)
    if in_use:
        raise ApiError(
            409,
            f"Cannot delete {kind} while one or more tasks still reference it",
        )

    table.delete_item(
        Key={"PK": user_pk(event), "SK": f"{config['prefix']}#{entity_id}"}
    )
    return response(204)


def reorder_statuses(event):
    body = parse_body(event)
    status_ids = body.get("statusIds")
    if not isinstance(status_ids, list) or not all(
        isinstance(status_id, str) and status_id for status_id in status_ids
    ):
        raise ApiError(400, "'statusIds' must be an array of status IDs")
    if len(status_ids) != len(set(status_ids)):
        raise ApiError(400, "'statusIds' cannot contain duplicates")

    existing = query_prefix(user_pk(event), "STATUS#")
    existing_ids = {item["statusId"] for item in existing}
    if set(status_ids) != existing_ids:
        raise ApiError(400, "'statusIds' must contain every status exactly once")

    by_id = {item["statusId"]: item for item in existing}
    with table.batch_writer() as batch:
        for index, status_id in enumerate(status_ids, start=1):
            item = by_id[status_id]
            item["position"] = index * 1000
            item["updatedAt"] = now_iso()
            batch.put_item(Item=item)

    return response(200, {"statusIds": status_ids})
