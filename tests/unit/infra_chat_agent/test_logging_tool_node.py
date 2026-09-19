import json

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from app.infra_chat_agent.workflows.reference import logging_tool_node as module_under_test


class _FakeToolNode:
    def __init__(self, _tools, handle_tool_errors=True):
        self.handle_tool_errors = handle_tool_errors
        self.seen_state = None

    async def ainvoke(self, state):
        self.seen_state = state
        return {"messages": []}


@pytest.mark.asyncio
async def test_injects_validate_params_conversation_state(monkeypatch):
    fake_tool_node = _FakeToolNode([], True)

    class _Factory:
        def __init__(self, _tools, handle_tool_errors=True):
            self.instance = fake_tool_node

        async def ainvoke(self, state):
            return await self.instance.ainvoke(state)

    monkeypatch.setattr(module_under_test, "ToolNode", _Factory)

    node = module_under_test.create_logging_tool_node([])
    state = {
        "tenant_id": "vance",
        "validate_params_state": {"tool_name": "CreateS3", "valid": {"name": "bucketa"}},
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call-1",
                        "name": "ValidateParams",
                        "args": {
                            "tool_name": "CreateS3",
                            "params": "{}",
                            "user_message": "test",
                        },
                    }
                ],
            )
        ],
    }

    await node(state)
    injected = fake_tool_node.seen_state["messages"][-1].tool_calls[0]["args"]["conversation_state"]
    assert injected == {"tool_name": "CreateS3", "valid": {"name": "bucketa"}}
    assert fake_tool_node.seen_state["messages"][-1].tool_calls[0]["args"]["tenant_id"] == "vance"


class _FakeToolNodeWithResult:
    def __init__(self, _tools, handle_tool_errors=True):
        self.handle_tool_errors = handle_tool_errors

    async def ainvoke(self, _state):
        payload = {
            "valid": [{"param": "name", "value": "bucketa"}],
            "invalid": {},
            "missing": [],
            "hallucinated": {},
            "conversation_state": {
                "tool_name": "CreateS3",
                "valid": {"name": "bucketa"},
            },
        }
        return {
            "messages": [
                ToolMessage(
                    content=[{"type": "text", "text": json.dumps(payload)}],
                    tool_call_id="call-1",
                    name="ValidateParams",
                )
            ]
        }


@pytest.mark.asyncio
async def test_extracts_validate_params_conversation_state(monkeypatch):
    monkeypatch.setattr(module_under_test, "ToolNode", _FakeToolNodeWithResult)

    node = module_under_test.create_logging_tool_node([])
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call-1",
                        "name": "ValidateParams",
                        "args": {
                            "tool_name": "CreateS3",
                            "params": "{}",
                            "user_message": "test",
                        },
                    }
                ],
            )
        ],
    }

    result = await node(state)
    assert result["validate_params_state"] == {
        "tool_name": "CreateS3",
        "valid": {"name": "bucketa"},
    }


@pytest.mark.asyncio
async def test_overwrites_validate_params_tenant_id_from_state(monkeypatch):
    fake_tool_node = _FakeToolNode([], True)

    class _Factory:
        def __init__(self, _tools, handle_tool_errors=True):
            self.instance = fake_tool_node

        async def ainvoke(self, state):
            return await self.instance.ainvoke(state)

    monkeypatch.setattr(module_under_test, "ToolNode", _Factory)

    node = module_under_test.create_logging_tool_node([])
    state = {
        "tenant_id": "aspora",
        "validate_params_state": {"tool_name": "CreateS3", "valid": {}},
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call-1",
                        "name": "ValidateParams",
                        "args": {
                            "tool_name": "CreateS3",
                            "params": "{}",
                            "user_message": "test",
                            "tenant_id": "wrong-tenant",
                        },
                    }
                ],
            )
        ],
    }

    await node(state)
    injected_tenant = fake_tool_node.seen_state["messages"][-1].tool_calls[0]["args"]["tenant_id"]
    assert injected_tenant == "aspora"
