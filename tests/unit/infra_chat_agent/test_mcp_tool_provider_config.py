from app.infra_chat_agent.config.mcp_tool_provider import (
    _get_mcp_servers_config,
    CREATE_FLOW_V2_RESOURCE_TOOL_MAP,
    DYNAMODB_TOOL_NAMES,
    get_dynamodb_mcp_tools_sync,
)


def test_validator_server_config_has_no_hardcoded_tenant_arg():
    servers = _get_mcp_servers_config()
    validator_args = servers["s3-creation-tools"]["args"]

    assert validator_args == ["-m", "app.infra_chat_agent.mcp_server.validator_server"]
    assert "vance" not in validator_args


def test_dynamodb_create_flow_uses_validator_tool_contract():
    assert DYNAMODB_TOOL_NAMES == ["ValidateParams"]
    assert CREATE_FLOW_V2_RESOURCE_TOOL_MAP["dynamodb_infrastructuretype_ref"] == {"ValidateParams"}

    tools = get_dynamodb_mcp_tools_sync()
    assert len(tools) == 1

    tool = tools[0]
    assert tool["name"] == "ValidateParams"

    schema = tool["inputSchema"]
    assert schema["properties"]["tool_name"]["enum"] == ["CreateDynamoDB"]
    assert "params" in schema["properties"]
    assert set(schema["required"]) == {"tool_name", "params", "user_message", "tenant_id", "conversation_state"}
