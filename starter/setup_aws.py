from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import zipfile
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


REGION = "us-east-1"
PROJECT_ROOT = Path(__file__).resolve().parent
LAMBDA_DIR = PROJECT_ROOT / "lambda"
PRODUCT_CATALOG = PROJECT_ROOT / "product_catalog.txt"
OPENAPI_SCHEMA = PROJECT_ROOT / "openapi-orders.json"
API_TARGET_CONFIG = PROJECT_ROOT / "gateway-orders-api-target.json"
REFUND_TARGET_CONFIG = PROJECT_ROOT / "gateway-refund-lambda-target.json"
API_STAGE = "prod"
API_NAME = "customer-support-orders-api"
ROLE_NAME = "customer-support-lambda-execution-role"
ORDER_FUNCTION = "order-tracker"
REFUND_FUNCTION = "refund-processor"
CONFIG = Config(retries={"max_attempts": 5, "mode": "adaptive"})


def client(service: str):
    return boto3.client(service, region_name=REGION, config=CONFIG)


def zip_python_file(path: Path) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(path, arcname=path.name)
    return buffer.getvalue()


def ensure_lambda_role(iam) -> str:
    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
    try:
        role = iam.get_role(RoleName=ROLE_NAME)["Role"]
    except iam.exceptions.NoSuchEntityException:
        role = iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(trust_policy),
            Description="Execution role for customer support sample Lambda functions.",
        )["Role"]
        iam.get_waiter("role_exists").wait(RoleName=ROLE_NAME)

    iam.attach_role_policy(
        RoleName=ROLE_NAME,
        PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
    )
    time.sleep(10)
    return role["Arn"]


def ensure_lambda_function(lambda_client, name: str, source: Path, handler: str, role_arn: str) -> str:
    code_bytes = zip_python_file(source)
    try:
        existing = lambda_client.get_function(FunctionName=name)["Configuration"]
        lambda_client.update_function_code(FunctionName=name, ZipFile=code_bytes, Publish=True)
        lambda_client.update_function_configuration(
            FunctionName=name,
            Runtime="python3.12",
            Handler=handler,
            Role=role_arn,
            Timeout=10,
            MemorySize=256,
            Architectures=["arm64"],
        )
        return existing["FunctionArn"]
    except lambda_client.exceptions.ResourceNotFoundException:
        created = lambda_client.create_function(
            FunctionName=name,
            Runtime="python3.12",
            Role=role_arn,
            Handler=handler,
            Code={"ZipFile": code_bytes},
            Description=f"Customer support sample function: {name}",
            Timeout=10,
            MemorySize=256,
            Publish=True,
            Architectures=["arm64"],
        )
        return created["FunctionArn"]


def find_rest_api(api_gateway) -> dict[str, Any]:
    paginator = api_gateway.get_paginator("get_rest_apis")
    for page in paginator.paginate():
        for item in page.get("items", []):
            if item.get("name") == API_NAME:
                return item
    return api_gateway.create_rest_api(
        name=API_NAME,
        description="Order lookup API for AgentCore Gateway MCP tools.",
        endpointConfiguration={"types": ["REGIONAL"]},
    )


def root_resource_id(api_gateway, api_id: str) -> str:
    resources = api_gateway.get_resources(restApiId=api_id)["items"]
    for resource in resources:
        if resource.get("path") == "/":
            return resource["id"]
    raise RuntimeError("API Gateway root resource was not found.")


def ensure_child_resource(api_gateway, api_id: str, parent_id: str, path_part: str) -> str:
    resources = api_gateway.get_resources(restApiId=api_id, limit=500)["items"]
    for resource in resources:
        if resource.get("parentId") == parent_id and resource.get("pathPart") == path_part:
            return resource["id"]
    return api_gateway.create_resource(restApiId=api_id, parentId=parent_id, pathPart=path_part)["id"]


def ensure_resource_path(api_gateway, api_id: str, parts: list[str]) -> str:
    current = root_resource_id(api_gateway, api_id)
    for part in parts:
        current = ensure_child_resource(api_gateway, api_id, current, part)
    return current


def put_get_method(api_gateway, api_id: str, resource_id: str, operation_name: str, path_params: list[str]):
    request_parameters = {f"method.request.path.{name}": True for name in path_params}
    try:
        api_gateway.put_method(
            restApiId=api_id,
            resourceId=resource_id,
            httpMethod="GET",
            authorizationType="NONE",
            operationName=operation_name,
            requestParameters=request_parameters,
        )
    except api_gateway.exceptions.ConflictException:
        api_gateway.update_method(
            restApiId=api_id,
            resourceId=resource_id,
            httpMethod="GET",
            patchOperations=[{"op": "replace", "path": "/operationName", "value": operation_name}],
        )


def put_lambda_proxy_integration(api_gateway, api_id: str, resource_id: str, lambda_arn: str):
    integration_uri = (
        f"arn:aws:apigateway:{REGION}:lambda:path/2015-03-31/functions/{lambda_arn}/invocations"
    )
    api_gateway.put_integration(
        restApiId=api_id,
        resourceId=resource_id,
        httpMethod="GET",
        type="AWS_PROXY",
        integrationHttpMethod="POST",
        uri=integration_uri,
    )


def allow_api_gateway(lambda_client, function_name: str, statement_id: str, source_arn: str):
    try:
        lambda_client.add_permission(
            FunctionName=function_name,
            StatementId=statement_id,
            Action="lambda:InvokeFunction",
            Principal="apigateway.amazonaws.com",
            SourceArn=source_arn,
        )
    except lambda_client.exceptions.ResourceConflictException:
        return


def configure_rest_api(api_gateway, lambda_client, account_id: str, order_lambda_arn: str) -> str:
    api = find_rest_api(api_gateway)
    api_id = api["id"]

    routes = [
        {
            "parts": ["orders", "{order_id}"],
            "operation": "get_order",
            "params": ["order_id"],
            "source_path": "/orders/*",
            "permission": "AllowGetOrder",
        },
        {
            "parts": ["customers", "{customer_id}", "orders"],
            "operation": "get_customer_orders",
            "params": ["customer_id"],
            "source_path": "/customers/*/orders",
            "permission": "AllowGetCustomerOrders",
        },
        {
            "parts": ["customers", "{customer_id}"],
            "operation": "get_customer",
            "params": ["customer_id"],
            "source_path": "/customers/*",
            "permission": "AllowGetCustomer",
        },
    ]

    for route in routes:
        resource_id = ensure_resource_path(api_gateway, api_id, route["parts"])
        put_get_method(api_gateway, api_id, resource_id, route["operation"], route["params"])
        put_lambda_proxy_integration(api_gateway, api_id, resource_id, order_lambda_arn)
        source_arn = f"arn:aws:execute-api:{REGION}:{account_id}:{api_id}/*/GET{route['source_path']}"
        allow_api_gateway(lambda_client, ORDER_FUNCTION, route["permission"], source_arn)

    api_gateway.create_deployment(
        restApiId=api_id,
        stageName=API_STAGE,
        description="Deployment for customer support AgentCore Gateway project.",
    )
    return f"https://{api_id}.execute-api.{REGION}.amazonaws.com/{API_STAGE}"


def ensure_bucket(s3, sts) -> str:
    account_id = sts.get_caller_identity()["Account"]
    bucket = f"customer-support-kb-{account_id}-{REGION}"
    try:
        s3.create_bucket(Bucket=bucket)
    except s3.exceptions.BucketAlreadyOwnedByYou:
        return bucket
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
            return bucket
        raise
    return bucket


def write_openapi_schema(api_url: str):
    schema = {
        "openapi": "3.0.1",
        "info": {"title": "Customer Support Orders API", "version": "1.0.0"},
        "servers": [{"url": api_url}],
        "paths": {
            "/orders/{order_id}": {
                "get": {
                    "operationId": "get_order",
                    "description": "Retrieve a single order by order_id.",
                    "parameters": [
                        {"name": "order_id", "in": "path", "required": True, "schema": {"type": "string"}}
                    ],
                    "responses": {"200": {"description": "Order lookup response"}},
                }
            },
            "/customers/{customer_id}/orders": {
                "get": {
                    "operationId": "get_customer_orders",
                    "description": "List orders for a customer.",
                    "parameters": [
                        {"name": "customer_id", "in": "path", "required": True, "schema": {"type": "string"}}
                    ],
                    "responses": {"200": {"description": "Customer orders response"}},
                }
            },
            "/customers/{customer_id}": {
                "get": {
                    "operationId": "get_customer",
                    "description": "Retrieve customer profile, loyalty tier, and points.",
                    "parameters": [
                        {"name": "customer_id", "in": "path", "required": True, "schema": {"type": "string"}}
                    ],
                    "responses": {"200": {"description": "Customer profile response"}},
                }
            },
        },
    }
    OPENAPI_SCHEMA.write_text(json.dumps(schema, indent=2), encoding="utf-8")


def refund_tool_schema() -> list[dict[str, Any]]:
    return [
        {
            "name": "process_refund",
            "description": "Process an automated refund for a delivered customer order.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "string",
                        "description": "Order identifier, for example ORD-002.",
                    },
                    "customer_id": {
                        "type": "string",
                        "description": "Customer identifier, for example CUST-001.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Reason the customer requested the refund.",
                    },
                },
                "required": ["order_id", "customer_id", "reason"],
            },
            "outputSchema": {
                "type": "object",
                "properties": {
                    "approved": {"type": "boolean"},
                    "refund_id": {"type": "string"},
                    "order_id": {"type": "string"},
                    "customer_id": {"type": "string"},
                    "refund_amount": {"type": "number"},
                    "status": {"type": "string"},
                    "estimated_posting_days": {"type": "integer"},
                    "processed_at": {"type": "string"},
                    "message": {"type": "string"},
                },
            },
        }
    ]


def write_gateway_target_configs(outputs: dict[str, str]):
    api_target = {
        "mcp": {
            "openApiSchema": {
                "s3": {
                    "uri": outputs["openapi_s3_uri"],
                    "bucketOwnerAccountId": outputs["account_id"],
                }
            }
        }
    }
    refund_target = {
        "mcp": {
            "lambda": {
                "lambdaArn": outputs["refund_processor_lambda_arn"],
                "toolSchema": {"inlinePayload": refund_tool_schema()},
            }
        }
    }
    API_TARGET_CONFIG.write_text(json.dumps(api_target, indent=2), encoding="utf-8")
    REFUND_TARGET_CONFIG.write_text(json.dumps(refund_target, indent=2), encoding="utf-8")


def upload_assets(s3, bucket: str) -> dict[str, str]:
    s3.upload_file(str(PRODUCT_CATALOG), bucket, "knowledge-base/product_catalog.txt")
    s3.upload_file(str(OPENAPI_SCHEMA), bucket, "gateway/openapi-orders.json")
    return {
        "catalog_s3_uri": f"s3://{bucket}/knowledge-base/product_catalog.txt",
        "openapi_s3_uri": f"s3://{bucket}/gateway/openapi-orders.json",
    }


def print_next_steps(outputs: dict[str, str]):
    commands = [
        "agentcore configure --entrypoint main.py --name customer-support-agent",
        "agentcore deploy",
        "agentcore invoke '{\"prompt\":\"Where is order ORD-001?\",\"customer_id\":\"CUST-001\",\"session_id\":\"test-order-001\"}'",
        "agentcore invoke '{\"prompt\":\"Please process a refund for ORD-002 because the item arrived damaged.\",\"customer_id\":\"CUST-001\",\"session_id\":\"test-refund-002\"}'",
        "agentcore invoke '{\"prompt\":\"What benefits do Platinum tier members receive?\",\"customer_id\":\"CUST-001\",\"session_id\":\"test-kb-platinum\"}'",
        "agentcore invoke '{\"prompt\":\"Hi, my name is Maya and I prefer email updates. Please remember that.\",\"customer_id\":\"CUST-001\",\"session_id\":\"memory-session-a\"}'",
        "sleep 35 && agentcore invoke '{\"prompt\":\"What is my name and preferred update channel?\",\"customer_id\":\"CUST-001\",\"session_id\":\"memory-session-b\"}'",
        "agentcore invoke '{\"prompt\":\"Calculate my loyalty discount as a Gold member with 4250 points on a 150 dollar order.\",\"customer_id\":\"CUST-001\",\"session_id\":\"test-discount-gold\"}'",
        "agentcore invoke '{\"prompt\":\"Use the browser tool to navigate to https://www.udacity.com and tell me the page title.\",\"customer_id\":\"CUST-001\",\"session_id\":\"test-browser-udacity\"}'",
    ]

    print("\nCreated resources")
    for key, value in outputs.items():
        print(f"{key}: {value}")

    print("\nKnowledge Base setup commands")
    print(
        "aws bedrock-agent create-knowledge-base "
        "--region us-east-1 "
        "--name customer-support-kb "
        "--role-arn <BEDROCK_KB_SERVICE_ROLE_ARN> "
        "--knowledge-base-configuration "
        "'{\"type\":\"VECTOR\",\"vectorKnowledgeBaseConfiguration\":{\"embeddingModelArn\":\"arn:aws:bedrock:us-east-1::foundation-model/amazon.titan-embed-text-v2:0\"}}' "
        "--storage-configuration '<YOUR_VECTOR_STORE_CONFIGURATION_JSON>'"
    )
    print(
        "aws bedrock-agent create-data-source "
        "--region us-east-1 "
        "--knowledge-base-id <KB_ID> "
        "--name product-catalog-source "
        f"--data-source-configuration '{{\"type\":\"S3\",\"s3Configuration\":{{\"bucketArn\":\"arn:aws:s3:::{outputs['bucket']}\"}}}}'"
    )
    print(
        "aws bedrock-agent start-ingestion-job "
        "--region us-east-1 "
        "--knowledge-base-id <KB_ID> "
        "--data-source-id <DATA_SOURCE_ID>"
    )

    print("\nAgentCore Gateway target commands")
    print(
        "aws bedrock-agentcore-control create-gateway-target "
        "--region us-east-1 "
        "--gateway-identifier <GATEWAY_ID> "
        "--name orders-api-target "
        "--target-configuration file://gateway-orders-api-target.json"
    )
    print(
        "aws bedrock-agentcore-control create-gateway-target "
        "--region us-east-1 "
        "--gateway-identifier <GATEWAY_ID> "
        "--name refund-lambda-target "
        "--target-configuration file://gateway-refund-lambda-target.json "
        "--credential-provider-configurations "
        "'[{\"credentialProviderType\":\"GATEWAY_IAM_ROLE\"}]'"
    )

    print("\nSet these environment variables before deploying the runtime")
    print(f"export GATEWAY_URL=<YOUR_AGENTCORE_GATEWAY_MCP_URL>")
    print("export KB_ID=<YOUR_KNOWLEDGE_BASE_ID>")
    print("export MEMORY_ID=<YOUR_AGENTCORE_MEMORY_ID>")
    print("export REGION=us-east-1")

    print("\nAgentCore deployment and verification commands")
    for command in commands:
        print(command)


def provision() -> dict[str, str]:
    iam = client("iam")
    lambda_client = client("lambda")
    api_gateway = client("apigateway")
    s3 = client("s3")
    sts = client("sts")

    account_id = sts.get_caller_identity()["Account"]
    role_arn = ensure_lambda_role(iam)
    order_arn = ensure_lambda_function(
        lambda_client,
        ORDER_FUNCTION,
        LAMBDA_DIR / "order_tracker.py",
        "order_tracker.handler",
        role_arn,
    )
    refund_arn = ensure_lambda_function(
        lambda_client,
        REFUND_FUNCTION,
        LAMBDA_DIR / "refund_processor.py",
        "refund_processor.handler",
        role_arn,
    )
    api_url = configure_rest_api(api_gateway, lambda_client, account_id, order_arn)
    write_openapi_schema(api_url)
    bucket = ensure_bucket(s3, sts)
    uris = upload_assets(s3, bucket)

    outputs = {
        "region": REGION,
        "account_id": account_id,
        "lambda_role_arn": role_arn,
        "order_tracker_lambda_arn": order_arn,
        "refund_processor_lambda_arn": refund_arn,
        "orders_api_url": api_url,
        "bucket": bucket,
        "catalog_s3_uri": uris["catalog_s3_uri"],
        "openapi_s3_uri": uris["openapi_s3_uri"],
    }
    write_gateway_target_configs(outputs)
    return outputs


def main() -> int:
    parser = argparse.ArgumentParser(description="Provision AWS resources for the AgentCore customer support project.")
    parser.add_argument("--print-only", action="store_true", help="Print deployment commands without creating resources.")
    args = parser.parse_args()

    try:
        outputs = (
            {
                "region": REGION,
                "account_id": "<ACCOUNT_ID>",
                "lambda_role_arn": "<LAMBDA_ROLE_ARN>",
                "order_tracker_lambda_arn": "<ORDER_TRACKER_LAMBDA_ARN>",
                "refund_processor_lambda_arn": "<REFUND_PROCESSOR_LAMBDA_ARN>",
                "orders_api_url": "<ORDERS_API_URL>",
                "bucket": "<S3_BUCKET>",
                "catalog_s3_uri": "<PRODUCT_CATALOG_S3_URI>",
                "openapi_s3_uri": "<OPENAPI_SCHEMA_S3_URI>",
            }
            if args.print_only
            else provision()
        )
        print_next_steps(outputs)
        return 0
    except ClientError as exc:
        print(f"AWS API error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Setup failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
