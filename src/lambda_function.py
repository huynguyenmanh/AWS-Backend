import logging

from botocore.exceptions import ClientError

import attachments
import entities
import filters
import profile_api
import statistics
import tasks
import transfer
from common import ApiError, error_response, response


logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _route(event):
    route_key = event.get("routeKey")
    routes = {
        "GET /profile": profile_api.get_profile,
        "PATCH /profile": profile_api.update_profile,
        "POST /profile/avatar/upload-url": profile_api.create_avatar_upload_url,
        "POST /profile/avatar/confirm": profile_api.confirm_avatar,
        "DELETE /profile/avatar": profile_api.delete_avatar,
        "DELETE /account": profile_api.delete_account,
        "POST /tasks": tasks.create_task,
        "GET /tasks": tasks.list_tasks,
        "GET /tasks/{taskId}": tasks.get_task,
        "PATCH /tasks/{taskId}": tasks.update_task,
        "DELETE /tasks/{taskId}": tasks.delete_task,
        "POST /tasks/{taskId}/attachments/upload-url": attachments.create_upload_url,
        "POST /tasks/{taskId}/attachments/{attachmentId}/confirm": attachments.confirm_upload,
        "GET /tasks/{taskId}/attachments": attachments.list_attachments,
        "DELETE /tasks/{taskId}/attachments/{attachmentId}": attachments.delete_attachment,
        "POST /statuses": lambda value: entities.create_entity(value, "status"),
        "GET /statuses": lambda value: entities.list_entities(value, "status"),
        "PATCH /statuses/{statusId}": lambda value: entities.update_entity(
            value, "status"
        ),
        "DELETE /statuses/{statusId}": lambda value: entities.delete_entity(
            value, "status"
        ),
        "PATCH /statuses/order": entities.reorder_statuses,
        "POST /categories": lambda value: entities.create_entity(value, "category"),
        "GET /categories": lambda value: entities.list_entities(value, "category"),
        "PATCH /categories/{categoryId}": lambda value: entities.update_entity(
            value, "category"
        ),
        "DELETE /categories/{categoryId}": lambda value: entities.delete_entity(
            value, "category"
        ),
        "POST /tags": lambda value: entities.create_entity(value, "tag"),
        "GET /tags": lambda value: entities.list_entities(value, "tag"),
        "PATCH /tags/{tagId}": lambda value: entities.update_entity(value, "tag"),
        "DELETE /tags/{tagId}": lambda value: entities.delete_entity(value, "tag"),
        "POST /filters": filters.create_filter,
        "GET /filters": filters.list_filters,
        "PATCH /filters/{filterId}": filters.update_filter,
        "DELETE /filters/{filterId}": filters.delete_filter,
        "POST /imports/preview": transfer.preview_import,
        "POST /imports/confirm": transfer.confirm_import,
        "GET /exports/tasks": transfer.export_tasks,
        "GET /statistics": statistics.get_statistics,
        "GET /statistics/export": statistics.export_statistics,
    }

    handler = routes.get(route_key)
    if not handler:
        raise ApiError(404, f"Route not found: {route_key}")
    return handler(event)


def lambda_handler(event, context):
    try:
        if event.get("requestContext", {}).get("http", {}).get("method") == "OPTIONS":
            return response(204)
        return _route(event)
    except ApiError as exc:
        return error_response(exc)
    except ClientError as exc:
        request_id = exc.response.get("ResponseMetadata", {}).get("RequestId")
        logger.exception("AWS service error; request_id=%s", request_id)
        return response(502, {"message": "An AWS service operation failed"})
    except Exception:
        logger.exception("Unhandled Lambda error")
        return response(500, {"message": "Internal server error"})
