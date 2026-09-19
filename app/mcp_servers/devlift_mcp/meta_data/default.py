"""Default tenant resource metadata — DEPRECATED.

Source of truth has moved to chat-bot-POC's tenant-keyed JSON metadata at
`backend/data/metadata/default/`. The legacy Python metadata is preserved
as a docstring at the bottom of this file for reference and easy revert.

IS_PAAS is retained because is_paas_tenant() in registry.py still reads it
to choose between PaaS (Jenkins) and Enterprise (GitOps PR) trigger response
shapes — chatbot does not currently surface this flag.

NOTE: eks_service is fully commented out — the chatbot does not yet have
an `eks_service_creation_form.json`, so service deployments via MCP are
temporarily unavailable.
"""

# Default tenants are PaaS — Jenkins deploy flow, polling, ALB URLs.
IS_PAAS: bool = True

# Empty stub — chatbot is the source of truth for the resource catalog.
RESOURCE_METADATA: dict = {}


_LEGACY_METADATA_FOR_REFERENCE = """
RESOURCE_METADATA: dict = {
    "eks_service": {
        "label": "EKS Service",
        "description": (
            "Configure and deploy an existing service to an EKS (Kubernetes) cluster. "
            "The service must already exist in the service registry. "
            "Creates a service config draft and queues it for GitOps deployment."
        ),
        "case_ref_code": "update_service",
        "infrastructuretype_ref_code": "eks_infrastructuretype_ref",
        "transaction_table": "service_config",
        "identifier_field": "service_name",
        "fields": [
            {
                "name": "service_name",
                "canonical_name": "service_name",
                "type": "string",
                "required": True,
                "description": (
                    "Name of the service to deploy. Must match an existing service "
                    "in the registry (exact match or with '-service' suffix). "
                    "Auto-fillable from package.json name, pyproject.toml [project].name, "
                    "or the project directory name."
                ),
            },
            {
                "name": "repository",
                "canonical_name": "repository",
                "type": "string",
                "required": True,
                "description": (
                    "Git repository in 'owner/repo' format. Do NOT pass the full "
                    "HTTPS URL. Auto-fill by running `git remote get-url origin` "
                    "and extracting only the owner/repo segment."
                ),
            },
            {
                "name": "branches",
                "canonical_name": "branches",
                "type": "string_array",
                "required": True,
                "description": (
                    "Branch names for the workflow trigger. Auto-fill the current branch "
                    "from `git branch --show-current`."
                ),
            },
            {
                "name": "language",
                "canonical_name": "language",
                "type": "string",
                "required": True,
                "description": (
                    "Programming language: 'java', 'go', 'python', or 'node'. "
                    "Auto-fillable from file extensions / pyproject.toml / go.mod / package.json / pom.xml."
                ),
            },
            {
                "name": "port",
                "canonical_name": "port",
                "type": "integer",
                "required": True,
                "description": (
                    "Container port. Defaults by language: java/go=8080, python=8000, node=3000. "
                    "Auto-fillable from entry-file scan (FastAPI port=, Express app.listen, etc.)."
                ),
            },
            {
                "name": "health",
                "canonical_name": "health",
                "type": "string",
                "required": True,
                "description": "Health check endpoint path, e.g. '/health'.",
            },
            {
                "name": "cpu",
                "canonical_name": "cpu_requested",
                "type": "string",
                "required": True,
                "description": "CPU request in millicores, e.g. '500m'. Max 2000m in v1.",
            },
            {
                "name": "memory",
                "canonical_name": "memory_requested",
                "type": "string",
                "required": True,
                "description": "Memory request, e.g. '512Mi'. Max 2000Mi in v1.",
            },
            {
                "name": "replica_count",
                "canonical_name": "replica_count",
                "type": "integer",
                "required": True,
                "description": "Number of pod replicas to run.",
            },
            {
                "name": "dockerfile_path",
                "canonical_name": "dockerfile_path",
                "type": "string",
                "required": False,
                "description": "Path to the Dockerfile in the repo (e.g. 'Dockerfile').",
            },
            {
                "name": "language_version",
                "canonical_name": "language_version",
                "type": "string",
                "required": False,
                "description": "Language version, e.g. '3.11' for Python, '17' for Java.",
            },
        ],
    },
    "postgres_server": {
        "label": "PostgreSQL Server",
        "description": "Provision a managed PostgreSQL server for the selected placement.",
        "case_ref_code": "k8s_postgres_create_server",
        "infrastructuretype_ref_code": "devlift_k8s_postgres_infrastructuretype_ref",
        "transaction_table": "infrastructure_mst",
        "identifier_field": "server_name",
        "needs_cluster": True,
        "fields": [
            {
                "name": "server_name",
                "canonical_name": "server_name",
                "type": "string",
                "required": True,
                "description": "Name of the PostgreSQL server (e.g. 'common-pg-server').",
            },
            {
                "name": "resources_preset",
                "canonical_name": "primary.resourcesPreset",
                "type": "string",
                "required": True,
                "description": "Resource size for the primary pod. Choose from the available options.",
                "options": [
                    {"code": "nano", "name": "Nano"},
                ],
            },
            {
                "name": "storage_size",
                "canonical_name": "primary.persistence.size",
                "type": "string",
                "required": True,
                "description": "Persistent storage size. Default: '5Gi'.",
                "options": [
                    {"code": "5Gi",   "name": "5 GB - Dev"},
                    {"code": "10Gi",  "name": "10 GB - Small"},
                    {"code": "20Gi",  "name": "20 GB - Medium"},
                    {"code": "50Gi",  "name": "50 GB - Large"},
                    {"code": "100Gi", "name": "100 GB - Production"},
                ],
            },
        ],
    },
    "dynamodb_table": {
        "label": "DynamoDB Table",
        "description": (
            "Provision an AWS DynamoDB table with a partition key, optional "
            "sort key, and optional TTL."
        ),
        "case_ref_code": "table_management",
        "infrastructuretype_ref_code": "dynamodb_infrastructuretype_ref",
        "transaction_table": "infrastructure_mst",
        "identifier_field": "table_name",
        "fields": [
            {
                "name": "table_name",
                "canonical_name": "identifier",
                "type": "string",
                "required": True,
                "description": "DynamoDB table name. 3-255 characters.",
            },
            {
                "name": "partition_key",
                "canonical_name": "partition_key",
                "type": "string",
                "required": True,
                "description": "Partition (hash) key attribute name.",
            },
            {
                "name": "partition_key_type",
                "canonical_name": "partition_key_type",
                "type": "string",
                "required": True,
                "description": "Partition key data type. S=String, N=Number, B=Binary.",
                "options": [
                    {"code": "S", "name": "String (S)"},
                    {"code": "N", "name": "Number (N)"},
                    {"code": "B", "name": "Binary (B)"},
                ],
            },
            {
                "name": "range_key",
                "canonical_name": "range_key",
                "type": "string",
                "required": False,
                "description": "Sort (range) key attribute name. Optional.",
            },
            {
                "name": "ttl_enabled",
                "canonical_name": "ttl_enabled",
                "type": "boolean",
                "required": True,
                "description": "Enable time-to-live (TTL) on the table. Default false.",
            },
            {
                "name": "ttl_attribute_name",
                "canonical_name": "ttl_attribute_name",
                "type": "string",
                "required": False,
                "description": "Attribute name used for TTL expiry timestamps.",
            },
        ],
    },
}
"""
