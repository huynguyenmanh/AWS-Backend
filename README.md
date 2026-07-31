# Todo and Note API

Serverless backend for a Cognito-authenticated Todo and Note application. The
backend runs in AWS Lambda behind API Gateway and stores user-owned records in
DynamoDB. Task images and profile avatars are stored privately in S3 and are
accessed with short-lived presigned URLs.

The task-tag feature has been removed. The supported workflow entities are
statuses and categories.

## Project structure

```text
todo-api-project/
├── api/
│   └── openapi.yaml
├── iam/
│   └── lambda-execution-policy.json
├── src/
│   ├── __init__.py
│   ├── attachments.py
│   ├── common.py
│   ├── entities.py
│   ├── filters.py
│   ├── lambda_function.py
│   ├── profile_api.py
│   ├── statistics.py
│   ├── tasks.py
│   └── transfer.py
├── .gitignore
└── README.md
```

## Implemented APIs

- Profile retrieval and updates
- Profile-avatar upload, confirmation, retrieval, and deletion
- Account deletion across Cognito, DynamoDB, and S3
- Task creation, drafts, retrieval, updates, optimistic version checks, and deletion
- Task search, workflow filters, date filters, sorting, and saved filters
- Task-attachment upload, confirmation, listing, download, and deletion
- User-defined statuses, status ordering, and categories
- JSON and CSV task import preview and confirmation
- JSON and CSV task export
- Statistics totals and grouping by status, category, or time

## Required deployment configuration

| Setting | Value |
|---|---|
| Region | `YOUR_AWS_REGION` |
| AWS account | `YOUR_AWS_ACCOUNT_ID` |
| DynamoDB table | `YOUR_TABLE_NAME` |
| S3 bucket | `YOUR_ATTACHMENTS_BUCKET` |
| Cognito user pool | `YOUR_USER_POOL_ID` |
| API Gateway | `YOUR_API_ID` |
| Lambda handler | `lambda_function.lambda_handler` |

The project intentionally contains placeholders instead of account-specific
resource identifiers. `api/openapi.yaml` uses this reusable server template:

```text
https://{apiId}.execute-api.{region}.amazonaws.com
```

Set the OpenAPI `apiId` and `region` variables for the target environment.
Include a stage path if the API uses a named stage.

## Lambda environment variables

Configure these values in **Lambda → Configuration → Environment variables**:

```text
TABLE_NAME=YOUR_TABLE_NAME
ATTACHMENTS_BUCKET=YOUR_ATTACHMENTS_BUCKET
USER_POOL_ID=YOUR_USER_POOL_ID
```

The source does not contain fallback resource identifiers. Lambda fails during
initialization with a clear error if any required variable is missing.

## Build the Lambda ZIP

The Python files must be at the root of the Lambda ZIP, because the modules
import one another by names such as `common`, `tasks`, and `entities`.

PowerShell, from the project directory:

```powershell
Compress-Archive -Path .\src\*.py -DestinationPath .\todo-backend.zip -Force
```

macOS or Linux:

```bash
cd src
zip -r ../todo-backend.zip *.py
cd ..
```

Upload `todo-backend.zip` to the Lambda function and set the handler to:

```text
lambda_function.lambda_handler
```

The Lambda runtime already includes `boto3`; the backend has no additional
third-party runtime dependencies.

## Lambda execution role

Attach the custom policy in `iam/lambda-execution-policy.json` to the Lambda
execution role. It is the updated form of the existing application policy and
adds the permissions required by active-account checks and account deletion:

```text
cognito-idp:AdminGetUser
cognito-idp:AdminUserGlobalSignOut
```

It also retains:

- DynamoDB item, query, and batch-write access for the configured table
- S3 object access under the configured bucket's `users/*` prefix
- S3 prefix listing for account cleanup
- Cognito disable and delete operations

The custom policy intentionally does not duplicate CloudWatch Logs actions.
Keep the AWS-managed `AWSLambdaBasicExecutionRole` policy attached to the role.
If that managed policy is absent, Lambda cannot create or write its CloudWatch
log streams.

The IAM document is a template. Before attaching it, replace
`YOUR_AWS_REGION`, `YOUR_AWS_ACCOUNT_ID`, `YOUR_TABLE_NAME`,
`YOUR_ATTACHMENTS_BUCKET`, and `YOUR_USER_POOL_ID`. Do not commit the completed
account-specific policy to a shared or public repository.

`GetUser` and `UpdateUserAttributes` are called with the authenticated user's
Cognito access token. The four `Admin...` operations in the custom policy are
the Cognito operations authorized through the Lambda execution role.

## API documentation boundary

`api/openapi.yaml` is an HTTP contract and documentation file based on the
supplied OpenAPI document. It has been updated only for the confirmed removal
of task tags:

- Removed the `tagId` task query parameter
- Removed `TagId` and `TagPathId`
- Removed `tagIds` from task input
- Removed `tagIds` from saved-filter fields
- Removed `tag` from statistics grouping

OpenAPI's `tags:` keyword remains because it groups operations into sections
such as Profile, Tasks, and Statistics. It is unrelated to task tags.

The supplied document does not contain AWS-specific
`x-amazon-apigateway-integration` or authorizer extensions. Consequently, it
documents the routes but is not a complete infrastructure-as-code deployment
definition. This project does not invent Lambda integration URIs, API IDs, app
client IDs, or stage settings that were not supplied.

## Authentication

All application routes require a Cognito access token:

```http
Authorization: Bearer ACCESS_TOKEN_JWT
```

The access token is a JWT whose payload contains:

```json
{
  "token_use": "access"
}
```

Do not substitute the Cognito ID token. The backend calls Cognito `GetUser`,
which requires the access token.

Every protected request also calls `AdminGetUser` to verify that the Cognito
account still exists and is enabled. This closes the gap where a previously
issued JWT could otherwise remain valid until its expiration time after account
deletion.


