from app.infra_chat_agent.chat_state import ChatState
from app.infra_chat_agent.utils.graph_utils import get_tenant_id
from app.infra_chat_agent.config.tenant_config import TENANT_CONFIG


def rbac_node(state: ChatState) -> ChatState:
    """
    Role-Based Access Control (RBAC) node.

    TEMPORARY: Pass-through mode - no state modifications.
    TODO: Implement actual RBAC checks based on user roles and permissions.

    Returns:
        Empty dict (pass-through)
    """

    # TODO: Implement actual RBAC logic
    # For now, pass-through without modifying state
    return {}
