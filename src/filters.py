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
    validate_date,
)


ALLOWED_FIELDS = {
    "title",
    "statusId",
    "categoryId",
    "date",
    "startDate",
    "recordState",
}
ALLOWED_OPERATORS = {
    "EQUALS",
    "NOT_EQUALS",
    "CONTAINS",
    "BEFORE",
    "AFTER",
    "BETWEEN",
    "IN",
    "IS_EMPTY",
}
MAX_FILTER_CONDITIONS = 20
MAX_EXPRESSION_DEPTH = 5


def _validate_condition(condition):
    if not isinstance(condition, dict):
        raise ApiError(400, "Every filter condition must be an object")
    field = condition.get("field")
    operator = condition.get("operator")
    if field not in ALLOWED_FIELDS:
        raise ApiError(400, f"Unsupported filter field: {field}")
    if operator not in ALLOWED_OPERATORS:
        raise ApiError(400, f"Unsupported filter operator: {operator}")

    has_value = "value" in condition
    value = condition.get("value")
    if operator != "IS_EMPTY" and not has_value:
        raise ApiError(400, f"'{operator}' requires a value")
    if operator == "CONTAINS" and not isinstance(value, str):
        raise ApiError(400, "'CONTAINS' requires a string value")
    if operator == "IN" and (not isinstance(value, list) or not value):
        raise ApiError(400, "'IN' requires a non-empty array value")
    if operator == "BETWEEN" and (
        not isinstance(value, list) or len(value) != 2
    ):
        raise ApiError(400, "'BETWEEN' requires an array containing two values")
    if operator in {"BEFORE", "AFTER", "BETWEEN"}:
        if field not in {"date", "startDate"}:
            raise ApiError(400, f"'{operator}' can only be used with date fields")
        values = value if operator == "BETWEEN" else [value]
        for entry in values:
            validate_date(entry, "filter value")
    if field == "title" and operator not in {
        "EQUALS",
        "NOT_EQUALS",
        "CONTAINS",
        "IN",
        "IS_EMPTY",
    }:
        raise ApiError(400, f"'{operator}' is not supported for title")

    cleaned = {"field": field, "operator": operator}
    if operator != "IS_EMPTY":
        cleaned["value"] = value
    return cleaned


def _validate_conditions(conditions):
    if not isinstance(conditions, list) or not conditions:
        raise ApiError(400, "'conditions' must be a non-empty array")
    if len(conditions) > MAX_FILTER_CONDITIONS:
        raise ApiError(
            400,
            f"A filter cannot contain more than {MAX_FILTER_CONDITIONS} conditions",
        )
    return [_validate_condition(condition) for condition in conditions]


def _validate_expression(expression, depth=0, counter=None):
    if depth > MAX_EXPRESSION_DEPTH:
        raise ApiError(
            400,
            f"A filter expression cannot exceed {MAX_EXPRESSION_DEPTH} levels",
        )
    if not isinstance(expression, dict):
        raise ApiError(400, "Every filter expression node must be an object")
    if counter is None:
        counter = {"conditions": 0}

    kind = expression.get("kind")
    if kind == "condition":
        counter["conditions"] += 1
        if counter["conditions"] > MAX_FILTER_CONDITIONS:
            raise ApiError(
                400,
                f"A filter cannot contain more than {MAX_FILTER_CONDITIONS} conditions",
            )
        return {"kind": "condition", **_validate_condition(expression)}

    if kind == "group":
        logic = expression.get("logic")
        children = expression.get("children")
        if logic not in {"AND", "OR"}:
            raise ApiError(400, "A filter group must use AND or OR")
        if not isinstance(children, list) or not children:
            raise ApiError(400, "A filter group must contain at least one expression")
        return {
            "kind": "group",
            "logic": logic,
            "children": [
                _validate_expression(child, depth + 1, counter)
                for child in children
            ],
        }

    if kind == "not":
        if "child" not in expression:
            raise ApiError(400, "A NOT expression requires a child expression")
        return {
            "kind": "not",
            "child": _validate_expression(expression["child"], depth + 1, counter),
        }

    raise ApiError(400, "Expression kind must be condition, group, or not")


def _flatten_conditions(expression):
    if expression["kind"] == "condition":
        return [
            {
                key: value
                for key, value in expression.items()
                if key in {"field", "operator", "value"}
            }
        ]
    if expression["kind"] == "not":
        return _flatten_conditions(expression["child"])
    result = []
    for child in expression["children"]:
        result.extend(_flatten_conditions(child))
    return result


def _condition_matches(task, condition):
    field = condition["field"]
    operator = condition["operator"]
    expected = condition.get("value")
    actual = task.get("titleNormalized", "") if field == "title" else task.get(field)
    if field == "title" and isinstance(expected, str):
        expected = expected.casefold()

    if operator == "EQUALS":
        return actual == expected
    if operator == "NOT_EQUALS":
        return actual != expected
    if operator == "CONTAINS":
        if isinstance(actual, str) and isinstance(expected, str):
            return expected in actual
        if isinstance(actual, list):
            return expected in actual
        return False
    if operator == "BEFORE":
        return actual is not None and expected is not None and actual < expected
    if operator == "AFTER":
        return actual is not None and expected is not None and actual > expected
    if operator == "BETWEEN":
        return (
            actual is not None
            and isinstance(expected, list)
            and len(expected) == 2
            and expected[0] <= actual <= expected[1]
        )
    if operator == "IN":
        return isinstance(expected, list) and actual in expected
    if operator == "IS_EMPTY":
        return actual in (None, "", [])
    return False


def _expression_matches(task, expression):
    kind = expression["kind"]
    if kind == "condition":
        return _condition_matches(task, expression)
    if kind == "not":
        return not _expression_matches(task, expression["child"])
    matches = [
        _expression_matches(task, child)
        for child in expression.get("children", [])
    ]
    return any(matches) if expression["logic"] == "OR" else all(matches)


def evaluate_filter(task, filter_item):
    if filter_item.get("expression"):
        return _expression_matches(task, filter_item["expression"])
    matches = [
        _condition_matches(task, condition)
        for condition in filter_item.get("conditions", [])
    ]
    if filter_item.get("logic", "AND") == "OR":
        return any(matches)
    return all(matches)


def apply_saved_filter(event, filter_id, tasks):
    item = get_owned_item(event, f"FILTER#{filter_id}")
    return [task for task in tasks if evaluate_filter(task, item)]


def create_filter(event):
    body = parse_body(event)
    name = require_string(body, "name", maximum=100)
    if "expression" in body:
        if "logic" in body or "conditions" in body:
            raise ApiError(
                400,
                "Send either 'expression' or legacy 'logic' and 'conditions', not both",
            )
        expression = _validate_expression(body["expression"])
        conditions = _flatten_conditions(expression)
        logic = (
            expression["logic"]
            if expression["kind"] == "group"
            else "AND"
        )
    else:
        expression = None
        logic = body.get("logic", "AND")
        if logic not in {"AND", "OR"}:
            raise ApiError(400, "'logic' must be AND or OR")
        conditions = _validate_conditions(body.get("conditions"))
    filter_id = str(uuid.uuid4())
    timestamp = now_iso()
    item = {
        "PK": user_pk(event),
        "SK": f"FILTER#{filter_id}",
        "entityType": "FILTER",
        "filterId": filter_id,
        "name": name,
        "logic": logic,
        "conditions": conditions,
        "createdAt": timestamp,
        "updatedAt": timestamp,
    }
    if expression:
        item["expression"] = expression
    table.put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
    return response(201, public_item(item))


def list_filters(event):
    items = query_prefix(user_pk(event), "FILTER#")
    return response(200, {"items": [public_item(item) for item in items]})


def update_filter(event):
    filter_id = path_parameter(event, "filterId")
    item = get_owned_item(event, f"FILTER#{filter_id}")
    body = parse_body(event)
    if "name" in body:
        item["name"] = require_string(body, "name", maximum=100)
    if "expression" in body:
        if "logic" in body or "conditions" in body:
            raise ApiError(
                400,
                "Send either 'expression' or legacy 'logic' and 'conditions', not both",
            )
        expression = _validate_expression(body["expression"])
        item["expression"] = expression
        item["conditions"] = _flatten_conditions(expression)
        item["logic"] = (
            expression["logic"]
            if expression["kind"] == "group"
            else "AND"
        )
    else:
        if "logic" in body:
            if body["logic"] not in {"AND", "OR"}:
                raise ApiError(400, "'logic' must be AND or OR")
            item["logic"] = body["logic"]
        if "conditions" in body:
            item["conditions"] = _validate_conditions(body["conditions"])
        if "logic" in body or "conditions" in body:
            item.pop("expression", None)
    item["updatedAt"] = now_iso()
    table.put_item(Item=item, ConditionExpression="attribute_exists(PK)")
    return response(200, public_item(item))


def delete_filter(event):
    filter_id = path_parameter(event, "filterId")
    get_owned_item(event, f"FILTER#{filter_id}")
    table.delete_item(Key={"PK": user_pk(event), "SK": f"FILTER#{filter_id}"})
    return response(204)
