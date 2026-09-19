#!/bin/bash
set -e

echo "============================================"
echo "  ObsTool Backend Entrypoint (ECS)"
echo "============================================"

# ============================================
# Load config from AWS Secrets Manager
# ============================================
# The infrastructure-v2 ecs layer does not inject secrets into the task
# definition (container_secrets = [] since commit 27e4062); it passes
# AWS_SECRETS_MANAGER_NAME and expects the service to fetch its own values,
# which is what every other Vance service does. Exported here so pydantic
# Settings sees them as plain env vars.
#
# No-op where the task definition already injects secrets via valueFrom
# (stage), because AWS_SECRETS_MANAGER_NAME is unset there.
if [ -n "${AWS_SECRETS_MANAGER_NAME}" ]; then
    echo ""
    echo "============================================"
    echo "  Loading secrets: ${AWS_SECRETS_MANAGER_NAME}"
    echo "============================================"

    if ! SECRET_JSON=$(aws secretsmanager get-secret-value \
            --secret-id "${AWS_SECRETS_MANAGER_NAME}" \
            --query SecretString --output text 2>&1); then
        echo "❌ ERROR: could not read ${AWS_SECRETS_MANAGER_NAME}"
        echo "   ${SECRET_JSON}"
        exit 1
    fi

    eval "$(printf '%s' "${SECRET_JSON}" | python3 -c '
import json, re, shlex, sys
data = json.load(sys.stdin)
valid = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
count = 0
for key, value in data.items():
    if not valid.match(key):
        continue
    print(f"export {key}={shlex.quote(str(value))}")
    count += 1
print(f"echo \"  loaded {count} keys\"")
')"
    echo "============================================"
else
    echo "⚠️  AWS_SECRETS_MANAGER_NAME not set - using existing environment"
fi

# ============================================
# Configure kubectl for EKS
# ============================================
# Default to hardcoded values if env vars not set
EKS_CLUSTER_NAME=${EKS_CLUSTER_NAME:-devlift-dev-cluster}
AWS_REGION=${AWS_REGION:-ap-south-1}

if [ -n "${EKS_CLUSTER_NAME}" ] && [ -n "${AWS_REGION}" ]; then
    echo ""
    echo "============================================"
    echo "  Configuring kubectl for EKS"
    echo "============================================"
    echo "EKS Cluster: ${EKS_CLUSTER_NAME}"
    echo "AWS Region: ${AWS_REGION}"

    # Update kubeconfig for EKS cluster (will use ECS Task Role)
    if aws eks update-kubeconfig --name "${EKS_CLUSTER_NAME}" --region "${AWS_REGION}"; then
        echo "✅ kubectl configured successfully for EKS cluster: ${EKS_CLUSTER_NAME}"

        # Verify kubectl connectivity
        if kubectl cluster-info > /dev/null 2>&1; then
            echo "✅ kubectl can connect to EKS cluster"
            kubectl version --short || true
        else
            echo "⚠️  WARNING: kubectl configured but cannot connect to cluster"
            echo "    This may be due to IAM permissions or network issues"
        fi
    else
        echo "❌ ERROR: Failed to configure kubectl for EKS cluster"
        echo "   Please check:"
        echo "   - ECS Task Role has eks:DescribeCluster permission"
        echo "   - ECS Task Role is mapped to Kubernetes RBAC"
        echo "   - EKS cluster name and region are correct"
    fi

    echo "============================================"
else
    echo "⚠️  Skipping kubectl configuration:"
    if [ -z "${EKS_CLUSTER_NAME}" ]; then
        echo "   - EKS_CLUSTER_NAME is not set"
    fi
    if [ -z "${AWS_REGION}" ]; then
        echo "   - AWS_REGION is not set"
    fi
    echo "   MCP server will not be able to deploy to EKS"
fi

# ============================================
# Database Migrations
# ============================================
echo ""
echo "============================================"
echo "  Running Database Migrations"
echo "============================================"

MAX_RETRIES=2
RETRY_COUNT=0

while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
    if alembic upgrade head; then
        echo "✅ Migrations completed successfully"
        break
    else
        RETRY_COUNT=$((RETRY_COUNT + 1))
        if [ $RETRY_COUNT -lt $MAX_RETRIES ]; then
            echo "⚠️  Migration failed, retrying in 5 seconds... (attempt $RETRY_COUNT/$MAX_RETRIES)"
            sleep 5
        else
            echo "❌ WARNING: Migration failed after $MAX_RETRIES attempts - starting app anyway"
            echo "   Please run migrations manually: docker exec <container> alembic upgrade head"
        fi
    fi
done

# ============================================
# Vector Database Seeding
# ============================================
# SEED_VECTORS=true      -> Check if empty and seed if needed (idempotent)
# SEED_VECTORS=force     -> Force reseed even if data exists (recreate)
# SEED_VECTORS unset     -> Skip seeding entirely

if [ "${QDRANT_ENABLED}" = "false" ] || [ "${QDRANT_ENABLED}" = "False" ] || [ -z "${QDRANT_ENABLED}" ]; then
    echo "⚠️  Skipping vector seeding (Qdrant is disabled: QDRANT_ENABLED=${QDRANT_ENABLED:-unset})"
elif [ -n "${SEED_VECTORS}" ]; then
    echo ""
    echo "============================================"
    echo "  Vector Database Seeding"
    echo "============================================"

    if [ "${SEED_VECTORS}" = "force" ]; then
        echo "Mode: FORCE RECREATE"
        python -m scripts.vector_db.seed_vectors --recreate || echo "⚠️  WARNING: Vector seeding failed"
    else
        echo "Mode: SEED IF EMPTY (idempotent)"
        python -m scripts.vector_db.seed_vectors || echo "⚠️  WARNING: Vector seeding failed"
    fi

    echo "============================================"
else
    echo "⚠️  Skipping vector seeding (set SEED_VECTORS=true to enable)"
fi

# ============================================
# Display Configuration Summary
# ============================================
echo ""
echo "============================================"
echo "  Configuration Summary"
echo "============================================"
echo "Environment Variables:"
echo "  - AWS_REGION: ${AWS_REGION:-not set}"
echo "  - EKS_CLUSTER_NAME: ${EKS_CLUSTER_NAME:-not set}"
echo "  - QDRANT_ENABLED: ${QDRANT_ENABLED:-not set}"
echo "  - REDIS_ENABLED: ${REDIS_ENABLED:-not set}"
echo "  - SLACK_SOCKET_MODE_ENABLED: ${SLACK_SOCKET_MODE_ENABLED:-not set}"
echo ""
echo "MCP Server Status:"
if [ -n "${EKS_CLUSTER_NAME}" ] && [ -n "${AWS_REGION}" ]; then
    echo "  ✅ EKS MCP Server: READY"
    echo "     - Can deploy services to ${EKS_CLUSTER_NAME}"
    echo "     - Endpoint: POST /mcp"
else
    echo "  ⚠️  EKS MCP Server: LIMITED"
    echo "     - Cannot deploy to EKS (missing config)"
    echo "     - MCP endpoint available but deployments will fail"
fi
echo "============================================"

echo ""
echo "🚀 Starting application..."
exec "$@"
