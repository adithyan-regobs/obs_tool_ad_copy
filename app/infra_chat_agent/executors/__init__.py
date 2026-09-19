from app.infra_chat_agent.executors.create_s3_executor import execute_create_s3
from app.infra_chat_agent.executors.create_sqs_executor import execute_create_sqs
from app.infra_chat_agent.executors.create_dynamodb_executor import execute_create_dynamodb
from app.infra_chat_agent.executors.create_kong_executor import execute_create_kong_route
from app.infra_chat_agent.executors.create_database_executor import execute_create_database

__all__ = [
    "execute_create_s3",
    "execute_create_sqs",
    "execute_create_dynamodb",
    "execute_create_kong_route",
    "execute_create_database",
]

EXECUTORS = {
    "CreateS3": execute_create_s3,
    "CreateSQS": execute_create_sqs,
    "CreateDynamoDB": execute_create_dynamodb,
    "CreateKongRoute": execute_create_kong_route,
    "CreateDatabase": execute_create_database,
}
