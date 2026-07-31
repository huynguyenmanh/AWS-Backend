import csv
import io

from common import ApiError, query_parameters, query_prefix, response, user_pk
from tasks import list_task_items


GROUPS = {"status", "category", "time"}


def calculate_statistics(event):
    params = query_parameters(event)
    group_by = params.get("groupBy")
    if group_by and group_by not in GROUPS:
        raise ApiError(400, "groupBy must be status, category, or time")

    tasks = [
        task
        # Keep the normal query filters and allow filterId to invoke the user's
        # saved filter before the statistics are grouped or exported.
        for task in list_task_items(event, include_custom_filter=True)
        if task.get("recordState") == "ACTIVE"
    ]
    result = {"total": len(tasks), "groupBy": group_by, "groups": []}
    if not group_by:
        return result

    counts = {}
    if group_by == "status":
        for task in tasks:
            key = task.get("statusId") or "UNASSIGNED"
            counts[key] = counts.get(key, 0) + 1
    elif group_by == "category":
        for task in tasks:
            key = task.get("categoryId") or "UNASSIGNED"
            counts[key] = counts.get(key, 0) + 1
    else:
        basis = params.get("basis", "createdAt")
        if basis not in {"createdAt", "date"}:
            raise ApiError(400, "time basis must be createdAt or date")
        for task in tasks:
            value = task.get(basis)
            if value:
                month = value[:7]
                counts[month] = counts.get(month, 0) + 1

    name_map = {}
    if group_by in {"status", "category"}:
        prefix = {"status": "STATUS#", "category": "CATEGORY#"}[group_by]
        id_field = {"status": "statusId", "category": "categoryId"}[group_by]
        name_map = {
            item[id_field]: item.get("name", item[id_field])
            for item in query_prefix(user_pk(event), prefix)
        }
        name_map["UNASSIGNED"] = "Unassigned"

    result["groups"] = [
        {"key": key, "name": name_map.get(key, key), "count": count}
        for key, count in sorted(counts.items())
    ]
    return result


def get_statistics(event):
    return response(200, calculate_statistics(event))


def export_statistics(event):
    params = query_parameters(event)
    export_format = params.get("format", "json").lower()
    statistics = calculate_statistics(event)
    if export_format == "json":
        return response(
            200,
            statistics,
            {"Content-Disposition": 'attachment; filename="statistics.json"'},
        )
    if export_format != "csv":
        raise ApiError(400, "'format' must be json or csv")

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["key", "name", "count"])
    writer.writeheader()
    if statistics["groups"]:
        writer.writerows(statistics["groups"])
    else:
        writer.writerow(
            {"key": "TOTAL", "name": "Total tasks", "count": statistics["total"]}
        )
    return response(
        200,
        output.getvalue(),
        {
            "Content-Type": "text/csv; charset=utf-8",
            "Content-Disposition": 'attachment; filename="statistics.csv"',
        },
    )
