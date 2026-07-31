import csv
import io
import json
import uuid

from common import (
    ApiError,
    now_iso,
    parse_body,
    public_item,
    query_parameters,
    query_prefix,
    response,
    table,
    user_id,
    user_pk,
)
from tasks import _apply_index_attributes, build_task_item, list_task_items


MAX_IMPORT_TASKS = 500
SCHEMA_VERSION = 2
IMPORT_FIELDS = {
    "title",
    "description",
    "recordState",
    "statusId",
    "categoryId",
    "date",
    "startDate",
}
NULLABLE_IMPORT_FIELDS = {"statusId", "categoryId", "date", "startDate"}


def _normalize_import_task(raw, row_number):
    if not isinstance(raw, dict):
        raise ApiError(400, f"Imported row {row_number} must be an object")
    result = {}
    for field in IMPORT_FIELDS:
        if field not in raw:
            continue
        value = raw[field]
        if field in NULLABLE_IMPORT_FIELDS and (
            value is None or (isinstance(value, str) and not value.strip())
        ):
            value = None
        result[field] = value
    result.setdefault("recordState", "ACTIVE")
    return result


def _definition(raw, kind, index):
    if not isinstance(raw, dict):
        raise ApiError(400, f"Imported {kind} definition {index} must be an object")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
        raise ApiError(400, f"Imported {kind} definition {index} has an invalid name")
    color = raw.get("color", "#64748B")
    if not isinstance(color, str):
        raise ApiError(400, f"Imported {kind} definition {index} has an invalid color")
    color = color.strip() or "#64748B"
    if len(color) > 20:
        raise ApiError(400, f"Imported {kind} definition {index} has an invalid color")
    reference = raw.get("ref") or f"{kind}-{index}"
    if not isinstance(reference, str):
        raise ApiError(400, f"Imported {kind} definition {index} has an invalid ref")
    reference = reference.strip()
    if not reference or len(reference) > 100:
        raise ApiError(400, f"Imported {kind} definition {index} has an invalid ref")
    result = {"ref": reference, "name": name.strip(), "color": color}
    if kind == "status" and isinstance(raw.get("position"), int):
        result["position"] = raw["position"]
    return result


def _definitions(raw_bundle, key, kind):
    raw_definitions = raw_bundle.get(key, [])
    if not isinstance(raw_definitions, list):
        raise ApiError(400, f"Imported JSON '{key}' must be an array")

    definitions = [
        _definition(raw, kind, index)
        for index, raw in enumerate(raw_definitions, 1)
    ]
    seen_refs = set()
    for definition in definitions:
        if definition["ref"] in seen_refs:
            raise ApiError(
                400,
                f"Imported {kind} definitions contain duplicate ref "
                f"'{definition['ref']}'",
            )
        seen_refs.add(definition["ref"])
    return definitions


def _portable_task(raw, row_number):
    if not isinstance(raw, dict):
        raise ApiError(400, f"Imported row {row_number} must be an object")
    task = _normalize_import_task(raw, row_number)
    for field in ("statusRef", "categoryRef"):
        if field in raw:
            value = raw[field]
            if value is not None and (not isinstance(value, str) or not value):
                raise ApiError(400, f"Imported row {row_number} has an invalid {field}")
            task[field] = value
    return task


def _bundle_from_json(parsed):
    if isinstance(parsed, list):
        return {
            "schemaVersion": 1,
            "statuses": [],
            "categories": [],
            "tasks": [
                _portable_task(row, index)
                for index, row in enumerate(parsed, 1)
            ],
        }
    if not isinstance(parsed, dict) or not isinstance(parsed.get("tasks"), list):
        raise ApiError(
            400,
            'Imported JSON must be an array or an object containing a "tasks" array',
        )
    schema_version = parsed.get("schemaVersion", 1)
    if not isinstance(schema_version, int) or schema_version not in {1, SCHEMA_VERSION}:
        raise ApiError(
            400,
            f"Unsupported import schemaVersion: {schema_version}",
        )
    statuses = _definitions(parsed, "statuses", "status")
    categories = _definitions(parsed, "categories", "category")
    return {
        "schemaVersion": schema_version,
        "statuses": statuses,
        "categories": categories,
        "tasks": [
            _portable_task(row, index)
            for index, row in enumerate(parsed["tasks"], 1)
        ],
    }


def _bundle_from_csv(content):
    reader = csv.DictReader(io.StringIO(content))
    if not reader.fieldnames or "title" not in reader.fieldnames:
        raise ApiError(400, "Imported CSV must contain a 'title' column")
    rows = list(reader)
    statuses = {}
    categories = {}
    tasks = []
    for index, raw in enumerate(rows, 1):
        task = _normalize_import_task(raw, index)
        task.pop("statusId", None)
        task.pop("categoryId", None)

        status_name = (raw.get("statusName") or "").strip()
        if status_name:
            key = status_name.casefold()
            if key not in statuses:
                statuses[key] = _definition(
                    {
                        "ref": f"status-{len(statuses) + 1}",
                        "name": status_name,
                        "color": raw.get("statusColor") or "#64748B",
                    },
                    "status",
                    len(statuses) + 1,
                )
            task["statusRef"] = statuses[key]["ref"]

        category_name = (raw.get("categoryName") or "").strip()
        if category_name:
            key = category_name.casefold()
            if key not in categories:
                categories[key] = _definition(
                    {
                        "ref": f"category-{len(categories) + 1}",
                        "name": category_name,
                        "color": raw.get("categoryColor") or "#64748B",
                    },
                    "category",
                    len(categories) + 1,
                )
            task["categoryRef"] = categories[key]["ref"]
        tasks.append(task)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "statuses": list(statuses.values()),
        "categories": list(categories.values()),
        "tasks": tasks,
    }


def _parse_import(body):
    import_format = body.get("format", "json")
    if not isinstance(import_format, str):
        raise ApiError(400, "'format' must be json or csv")
    import_format = import_format.lower()
    content = body.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ApiError(400, "'content' must be a non-empty string")
    if len(content.encode("utf-8")) > 1_000_000:
        raise ApiError(413, "Import preview is limited to 1 MB")

    if import_format == "json":
        try:
            bundle = _bundle_from_json(json.loads(content))
        except json.JSONDecodeError as exc:
            raise ApiError(400, "Imported JSON is invalid") from exc
    elif import_format == "csv":
        bundle = _bundle_from_csv(content)
    else:
        raise ApiError(400, "'format' must be json or csv")

    if len(bundle["tasks"]) > MAX_IMPORT_TASKS:
        raise ApiError(400, f"An import cannot exceed {MAX_IMPORT_TASKS} tasks")
    return bundle


def _entity_plan(event, definitions, kind, create=False):
    prefix = "STATUS" if kind == "status" else "CATEGORY"
    id_field = "statusId" if kind == "status" else "categoryId"
    existing = query_prefix(user_pk(event), f"{prefix}#")
    by_name = {
        (item.get("nameNormalized") or item.get("name", "").casefold()): item
        for item in existing
    }
    mapping = {}
    reused = []
    created = []
    planned_by_name = {}
    next_position = (len(existing) + 1) * 1000

    for definition in definitions:
        normalized = definition["name"].strip().casefold()
        item = by_name.get(normalized)
        if item:
            mapping[definition["ref"]] = item[id_field]
            reused.append(
                {
                    "ref": definition["ref"],
                    "name": item["name"],
                    id_field: item[id_field],
                }
            )
            continue

        if normalized in planned_by_name:
            mapping[definition["ref"]] = planned_by_name[normalized]
            continue

        planned_id = None
        if create:
            entity_id = str(uuid.uuid4())
            timestamp = now_iso()
            item = {
                "PK": user_pk(event),
                "SK": f"{prefix}#{entity_id}",
                "entityType": prefix,
                id_field: entity_id,
                "name": definition["name"],
                "nameNormalized": normalized,
                "color": definition["color"],
                "createdAt": timestamp,
                "updatedAt": timestamp,
            }
            if kind == "status":
                item["position"] = next_position
                next_position += 1000
            table.put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
            mapping[definition["ref"]] = entity_id
            by_name[normalized] = item
            planned_id = entity_id
        else:
            mapping[definition["ref"]] = None
        planned_by_name[normalized] = planned_id
        created.append(
            {
                "ref": definition["ref"],
                "name": definition["name"],
                "color": definition["color"],
            }
        )
    return mapping, {"reuse": reused, "create": created}


def _resolve_task(raw, row_number, status_map, category_map):
    task = _normalize_import_task(raw, row_number)
    if raw.get("statusRef"):
        if raw["statusRef"] not in status_map:
            raise ApiError(400, f"Imported row {row_number} has an unknown statusRef")
        task["statusId"] = status_map[raw["statusRef"]]
    if raw.get("categoryRef"):
        if raw["categoryRef"] not in category_map:
            raise ApiError(400, f"Imported row {row_number} has an unknown categoryRef")
        task["categoryId"] = category_map[raw["categoryRef"]]
    return task


def preview_import(event):
    bundle = _parse_import(parse_body(event))
    status_map, status_plan = _entity_plan(
        event, bundle["statuses"], "status", create=False
    )
    category_map, category_plan = _entity_plan(
        event, bundle["categories"], "category", create=False
    )

    # New definitions do not exist during preview. Map them to null only for
    # task-field validation; confirm creates them before building final tasks.
    for definition in bundle["statuses"]:
        status_map.setdefault(definition["ref"], None)
    for definition in bundle["categories"]:
        category_map.setdefault(definition["ref"], None)

    valid = []
    errors = []
    owner_id = user_id(event)
    for index, raw in enumerate(bundle["tasks"], 1):
        try:
            resolved = _resolve_task(
                raw, index, status_map, category_map
            )
            build_task_item(owner_id, resolved)
            valid.append(raw)
        except ApiError as exc:
            errors.append({"row": index, "message": exc.message})

    preview_bundle = {**bundle, "tasks": valid}
    return response(
        200,
        {
            "tasks": valid,
            "bundle": preview_bundle,
            "workflow": {
                "statuses": status_plan,
                "categories": category_plan,
            },
            "validCount": len(valid),
            "invalidCount": len(errors),
            "errors": errors,
        },
    )


def confirm_import(event):
    body = parse_body(event)
    raw_bundle = body.get("bundle")
    if raw_bundle is None and isinstance(body.get("tasks"), list):
        raw_bundle = {"schemaVersion": 1, "tasks": body["tasks"]}
    bundle = _bundle_from_json(raw_bundle)
    if not bundle["tasks"] or len(bundle["tasks"]) > MAX_IMPORT_TASKS:
        raise ApiError(400, f"Import must contain 1 to {MAX_IMPORT_TASKS} tasks")

    # Validate every task before creating workflow entities. This avoids
    # creating statuses/categories and only then discovering an invalid task.
    preview_status_map, _ = _entity_plan(
        event, bundle["statuses"], "status", create=False
    )
    preview_category_map, _ = _entity_plan(
        event, bundle["categories"], "category", create=False
    )
    for definition in bundle["statuses"]:
        preview_status_map.setdefault(definition["ref"], None)
    for definition in bundle["categories"]:
        preview_category_map.setdefault(definition["ref"], None)
    owner_id = user_id(event)
    items = []
    for index, raw in enumerate(bundle["tasks"], 1):
        items.append(
            build_task_item(
                owner_id,
                _resolve_task(
                    raw,
                    index,
                    preview_status_map,
                    preview_category_map,
                ),
            )
        )

    status_map, _ = _entity_plan(event, bundle["statuses"], "status", create=True)
    category_map, _ = _entity_plan(
        event, bundle["categories"], "category", create=True
    )
    # The tasks were already validated above. Attach the final owner-local IDs
    # directly so a just-created workflow entity does not depend on an
    # eventually consistent DynamoDB read.
    for index, (item, raw) in enumerate(zip(items, bundle["tasks"]), 1):
        resolved = _resolve_task(raw, index, status_map, category_map)
        item["statusId"] = resolved.get("statusId")
        item["categoryId"] = resolved.get("categoryId")
        _apply_index_attributes(item, owner_id)

    with table.batch_writer() as batch:
        for item in items:
            batch.put_item(Item=item)
    return response(
        201,
        {"createdCount": len(items), "tasks": [public_item(item) for item in items]},
    )


def _export_bundle(event, items):
    statuses = {
        item["statusId"]: item
        for item in query_prefix(user_pk(event), "STATUS#")
    }
    categories = {
        item["categoryId"]: item
        for item in query_prefix(user_pk(event), "CATEGORY#")
    }
    referenced_status_ids = {
        task["statusId"]
        for task in items
        if task.get("statusId") in statuses
    }
    referenced_category_ids = {
        task["categoryId"]
        for task in items
        if task.get("categoryId") in categories
    }
    status_ids = sorted(
        referenced_status_ids,
        key=lambda entity_id: (
            statuses[entity_id].get("position", 1_000_000_000),
            statuses[entity_id].get("name", "").casefold(),
            entity_id,
        ),
    )
    category_ids = sorted(
        referenced_category_ids,
        key=lambda entity_id: (
            categories[entity_id].get("name", "").casefold(),
            entity_id,
        ),
    )
    status_refs = {
        entity_id: f"status-{index}"
        for index, entity_id in enumerate(status_ids, 1)
    }
    category_refs = {
        entity_id: f"category-{index}"
        for index, entity_id in enumerate(category_ids, 1)
    }
    portable_tasks = []
    for task in items:
        value = {
            field: task.get(field)
            for field in (
                "title",
                "description",
                "recordState",
                "date",
                "startDate",
            )
        }
        if task.get("statusId") in status_refs:
            value["statusRef"] = status_refs[task["statusId"]]
        if task.get("categoryId") in category_refs:
            value["categoryRef"] = category_refs[task["categoryId"]]
        portable_tasks.append(value)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "statuses": [
            {
                "ref": status_refs[entity_id],
                "name": statuses[entity_id]["name"],
                "color": statuses[entity_id].get("color", "#64748B"),
                "position": statuses[entity_id].get("position"),
            }
            for entity_id in status_ids
        ],
        "categories": [
            {
                "ref": category_refs[entity_id],
                "name": categories[entity_id]["name"],
                "color": categories[entity_id].get("color", "#64748B"),
            }
            for entity_id in category_ids
        ],
        "tasks": portable_tasks,
    }


def export_tasks(event):
    params = query_parameters(event)
    export_format = params.get("format", "json").lower()
    items = [public_item(item) for item in list_task_items(event)]
    bundle = _export_bundle(event, items)

    if export_format == "json":
        return response(
            200,
            bundle,
            {"Content-Disposition": 'attachment; filename="tasks.json"'},
        )
    if export_format != "csv":
        raise ApiError(400, "'format' must be json or csv")

    status_by_ref = {item["ref"]: item for item in bundle["statuses"]}
    category_by_ref = {item["ref"]: item for item in bundle["categories"]}
    fields = [
        "title",
        "description",
        "recordState",
        "statusName",
        "statusColor",
        "categoryName",
        "categoryColor",
        "date",
        "startDate",
    ]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for task in bundle["tasks"]:
        status = status_by_ref.get(task.get("statusRef"), {})
        category = category_by_ref.get(task.get("categoryRef"), {})
        writer.writerow(
            {
                "title": task.get("title"),
                "description": task.get("description"),
                "recordState": task.get("recordState"),
                "statusName": status.get("name", ""),
                "statusColor": status.get("color", ""),
                "categoryName": category.get("name", ""),
                "categoryColor": category.get("color", ""),
                "date": task.get("date"),
                "startDate": task.get("startDate"),
            }
        )
    return response(
        200,
        output.getvalue(),
        {
            "Content-Type": "text/csv; charset=utf-8",
            "Content-Disposition": 'attachment; filename="tasks.csv"',
        },
    )