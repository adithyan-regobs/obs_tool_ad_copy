import json
import pytest
from typing import Literal
from pydantic import BaseModel

from app.infra_chat_agent.mcp_server.models import TENANT_MODELS
from app.infra_chat_agent.mcp_server import validator_server
from app.infra_chat_agent.config.resource_meta_repo import resource_meta_repo
from app.infra_chat_agent.config.config_models import TenantId, InfraTypeCode


@pytest.mark.asyncio
async def test_validate_params_collects_and_merges_state():
    state = {"tool_name": None, "valid": {}}

    result_1, state_1 = await validator_server.validate_params_structured(
        tool_name="CreateS3",
        new_params={"name": "bucketa"},
        user_message="name bucketa",
        tenant_models=TENANT_MODELS["vance"],
        conversation_state=state,
        tenant_id="vance",
        thread_id="t-1",
    )

    assert "conversation_state" in result_1
    assert state_1["tool_name"] == "CreateS3"
    assert state_1["valid"]["name"] == "bucketa"

    result_2, state_2 = await validator_server.validate_params_structured(
        tool_name="CreateS3",
        new_params={"region": "london"},
        user_message="region london",
        tenant_models=TENANT_MODELS["vance"],
        conversation_state=state_1,
        tenant_id="vance",
        thread_id="t-1",
    )

    assert "conversation_state" in result_2
    assert state_2["valid"]["name"] == "bucketa"
    assert state_2["valid"]["region"] == "london"


@pytest.mark.asyncio
async def test_validate_params_rejects_invalid_value_without_merging():
    state = {"tool_name": "CreateS3", "valid": {"name": "bucketa", "region": "london"}}

    result, next_state = await validator_server.validate_params_structured(
        tool_name="CreateS3",
        new_params={"environment": "invalid-env"},
        user_message="environment invalid-env",
        tenant_models=TENANT_MODELS["vance"],
        conversation_state=state,
        tenant_id="vance",
        thread_id="t-2",
    )

    assert "environment" in result["invalid"]
    assert "environment" not in next_state["valid"]
    assert next_state["valid"]["name"] == "bucketa"


@pytest.mark.asyncio
async def test_validate_params_resets_state_on_tool_change():
    state = {"tool_name": "CreateS3", "valid": {"name": "bucketa", "region": "london"}}

    result, next_state = await validator_server.validate_params_structured(
        tool_name="CreateSQS",
        new_params={"name": "queuea"},
        user_message="queuea",
        tenant_models=TENANT_MODELS["vance"],
        conversation_state=state,
        tenant_id="vance",
        thread_id="t-3",
    )

    assert "conversation_state" in result
    assert next_state["tool_name"] == "CreateSQS"
    assert next_state["valid"] == {"name": "queuea"}


@pytest.mark.asyncio
async def test_validate_params_cross_account_required_when_replication_true():
    state = {
        "tool_name": "CreateS3",
        "valid": {
            "name": "bucketa",
            "region": "london",
            "product": "core",
            "environment": "qa",
            "replication": True,
            "version": False,
        },
    }

    result, next_state = await validator_server.validate_params_structured(
        tool_name="CreateS3",
        new_params={},
        user_message="continue",
        tenant_models=TENANT_MODELS["vance"],
        conversation_state=state,
        tenant_id="vance",
        thread_id="t-4",
    )

    cross_entry = next((item for item in result["missing"] if item["param"] == "crossAccountId"), None)
    assert cross_entry is not None
    assert cross_entry["required"] is True


@pytest.mark.asyncio
async def test_validate_params_cross_account_id_must_be_12_digits():
    state = {"tool_name": "CreateS3", "valid": {"name": "bucketa"}}

    result, next_state = await validator_server.validate_params_structured(
        tool_name="CreateS3",
        new_params={"crossAccountId": "4567"},
        user_message="crossAccountId 4567",
        tenant_models=TENANT_MODELS["vance"],
        conversation_state=state,
        tenant_id="vance",
        thread_id="t-4b",
    )

    assert "crossAccountId" in result["invalid"]
    assert "crossAccountId" not in next_state["valid"]


@pytest.mark.asyncio
async def test_validate_params_replication_is_optional(monkeypatch):
    async def _fake_executor(_validated, _tenant_id):
        return {"status": "success", "is_ready": True, "message": "done"}

    monkeypatch.setitem(validator_server.EXECUTORS, "CreateS3", _fake_executor)

    state = {
        "tool_name": "CreateS3",
        "valid": {
            "name": "bucketa",
            "region": "london",
            "product": "core",
            "environment": "qa",
            "version": False,
        },
    }

    result, _next_state = await validator_server.validate_params_structured(
        tool_name="CreateS3",
        new_params={},
        user_message="continue",
        tenant_models=TENANT_MODELS["vance"],
        conversation_state=state,
        tenant_id="vance",
        thread_id="t-4c",
    )

    assert result["status"] == "success"
    assert result["is_ready"] is True


@pytest.mark.asyncio
async def test_validate_params_normalizes_region_in_placement_payload():
    state = {"tool_name": None, "valid": {}}

    result, _next_state = await validator_server.validate_params_structured(
        tool_name="CreateS3",
        new_params={"region": "mumbai"},
        user_message="region mumbai",
        tenant_models=TENANT_MODELS["vance"],
        conversation_state=state,
        tenant_id="vance",
        thread_id="t-region-1",
    )

    assert result["placement_parameters"]["geo_loc_mst_code"] == "region-aspora-mumbai"


@pytest.mark.asyncio
async def test_validate_params_resolves_product_to_applications_code():
    state = {"tool_name": None, "valid": {}}

    result, _next_state = await validator_server.validate_params_structured(
        tool_name="CreateS3",
        new_params={"product": "core"},
        user_message="product core",
        tenant_models=TENANT_MODELS["vance"],
        conversation_state=state,
        tenant_id="vance",
        thread_id="t-product-1",
    )

    expected_app_code = resource_meta_repo.resolve_placement_value(
        TenantId("vance"),
        InfraTypeCode("s3_infrastructuretype_ref"),
        "applications_mst_code",
        "core",
    )
    assert result["placement_parameters"]["applications_mst_code"] == expected_app_code


@pytest.mark.asyncio
async def test_validate_params_dynamodb_resolves_product_to_applications_code():
    state = {"tool_name": None, "valid": {}}

    result, _next_state = await validator_server.validate_params_structured(
        tool_name="CreateDynamoDB",
        new_params={"product": "core"},
        user_message="product core",
        tenant_models=TENANT_MODELS["vance"],
        conversation_state=state,
        tenant_id="vance",
        thread_id="t-dynamodb-product-1",
    )

    expected_app_code = resource_meta_repo.resolve_placement_value(
        TenantId("vance"),
        InfraTypeCode("dynamodb_infrastructuretype_ref"),
        "applications_mst_code",
        "core",
    )
    assert result["placement_parameters"]["applications_mst_code"] == expected_app_code


@pytest.mark.asyncio
async def test_validate_params_rejects_unresolvable_product(monkeypatch):
    monkeypatch.setattr(
        validator_server,
        "_resolve_product_application_code",
        lambda *_args, **_kwargs: (None, None),
    )

    result, next_state = await validator_server.validate_params_structured(
        tool_name="CreateS3",
        new_params={"product": "core"},
        user_message="product core",
        tenant_models=TENANT_MODELS["vance"],
        conversation_state={"tool_name": None, "valid": {}},
        tenant_id="vance",
        thread_id="t-product-unresolved-1",
    )

    assert "product" in result["invalid"]
    assert "product" not in next_state["valid"]


@pytest.mark.asyncio
async def test_validate_params_keeps_environment_enum_key():
    state = {"tool_name": None, "valid": {}}

    result, _next_state = await validator_server.validate_params_structured(
        tool_name="CreateS3",
        new_params={"environment": "stage"},
        user_message="environment stage",
        tenant_models=TENANT_MODELS["vance"],
        conversation_state=state,
        tenant_id="vance",
        thread_id="t-env-1",
    )

    assert result["placement_parameters"]["environment_enum"] == "stage"


@pytest.mark.asyncio
async def test_validate_params_dynamodb_normalizes_region_in_placement_payload():
    state = {"tool_name": None, "valid": {}}

    result, _next_state = await validator_server.validate_params_structured(
        tool_name="CreateDynamoDB",
        new_params={"region": "mumbai"},
        user_message="region mumbai",
        tenant_models=TENANT_MODELS["vance"],
        conversation_state=state,
        tenant_id="vance",
        thread_id="t-dynamodb-region-1",
    )

    assert result["placement_parameters"]["geo_loc_mst_code"] == "region-aspora-mumbai"


@pytest.mark.asyncio
async def test_validate_params_accepts_short_region_when_model_uses_full_codes(monkeypatch):
    class _CreateS3FullRegionModel(BaseModel):
        name: str
        region: Literal["region-aspora-mumbai", "region-aspora-london"]
        product: Literal["core"]
        environment: Literal["stage"]
        replication: bool = False

    async def _fake_executor(_validated, _tenant_id):
        return {"status": "success", "is_ready": True, "message": "done"}

    monkeypatch.setitem(validator_server.EXECUTORS, "CreateS3", _fake_executor)

    result, next_state = await validator_server.validate_params_structured(
        tool_name="CreateS3",
        new_params={
            "name": "bucketa",
            "region": "mumbai",
            "product": "core",
            "environment": "stage",
            "replication": False,
        },
        user_message="create bucketa in mumbai core stage with replication false",
        tenant_models={"CreateS3": _CreateS3FullRegionModel},
        conversation_state={"tool_name": None, "valid": {}},
        tenant_id="vance",
        thread_id="t-region-2",
    )

    assert result["status"] == "success"
    assert next_state["valid"]["region"] == "region-aspora-mumbai"


@pytest.mark.asyncio
async def test_validate_params_auto_execute_keeps_state_for_updates(monkeypatch):
    async def _fake_executor(_validated, _tenant_id):
        return {"status": "success", "is_ready": True, "message": "done"}

    monkeypatch.setitem(validator_server.EXECUTORS, "CreateS3", _fake_executor)

    state = {
        "tool_name": "CreateS3",
        "valid": {
            "name": "bucketa",
            "region": "london",
            "product": "core",
            "environment": "qa",
            "version": False,
            "replication": False,
            "crossAccountId": "123456789012",
        },
    }

    result, next_state = await validator_server.validate_params_structured(
        tool_name="CreateS3",
        new_params={},
        user_message="continue",
        tenant_models=TENANT_MODELS["vance"],
        conversation_state=state,
        tenant_id="vance",
        thread_id="t-5",
    )

    assert result["status"] == "success"
    assert result["conversation_state"]["tool_name"] == "CreateS3"
    assert result["conversation_state"]["valid"]["name"] == "bucketa"
    assert next_state["tool_name"] == "CreateS3"
    assert next_state["valid"]["name"] == "bucketa"

    # Simulate user update after is_ready=true. Prior values must be preserved.
    result_2, next_state_2 = await validator_server.validate_params_structured(
        tool_name="CreateS3",
        new_params={"version": True},
        user_message="set version true",
        tenant_models=TENANT_MODELS["vance"],
        conversation_state=next_state,
        tenant_id="vance",
        thread_id="t-5",
    )

    assert result_2["status"] == "success"
    assert next_state_2["valid"]["name"] == "bucketa"
    assert next_state_2["valid"]["version"] is True


@pytest.mark.asyncio
async def test_validate_params_dynamodb_auto_exec_with_required_fields(monkeypatch):
    async def _fake_executor(_validated, _tenant_id):
        return {"status": "success", "is_ready": True, "message": "done"}

    monkeypatch.setitem(validator_server.EXECUTORS, "CreateDynamoDB", _fake_executor)

    result, next_state = await validator_server.validate_params_structured(
        tool_name="CreateDynamoDB",
        new_params={
            "identifier": "users-table",
            "partition_key": "user_id",
            "partition_key_type": "S",
            "product": "core",
            "region": "mumbai",
            "environment": "qa",
        },
        user_message="identifier users-table partition_key user_id partition_key_type S product core region mumbai environment qa",
        tenant_models=TENANT_MODELS["vance"],
        conversation_state={"tool_name": None, "valid": {}},
        tenant_id="vance",
        thread_id="t-dynamodb-ready-1",
    )

    assert result["status"] == "success"
    assert result["is_ready"] is True
    assert next_state["tool_name"] == "CreateDynamoDB"
    assert next_state["valid"]["identifier"] == "users-table"
    assert next_state["valid"]["partition_key_type"] == "S"


@pytest.mark.asyncio
async def test_validate_params_dynamodb_partition_key_type_alias_is_invalid_without_validator_normalization():
    result, next_state = await validator_server.validate_params_structured(
        tool_name="CreateDynamoDB",
        new_params={"partition_key_type": "string"},
        user_message="partition_key_type string",
        tenant_models=TENANT_MODELS["vance"],
        conversation_state={"tool_name": None, "valid": {}},
        tenant_id="vance",
        thread_id="t-dynamodb-type-1",
    )

    assert "partition_key_type" in result["invalid"]
    assert "allowed options" in result["invalid"]["partition_key_type"]["reason"]
    assert "partition_key_type" not in next_state["valid"]


def test_tenant_models_includes_aspora_same_as_vance():
    assert "aspora" in TENANT_MODELS
    assert TENANT_MODELS["aspora"]["CreateS3"] is TENANT_MODELS["vance"]["CreateS3"]
    assert TENANT_MODELS["aspora"]["CreateSQS"] is TENANT_MODELS["vance"]["CreateSQS"]
    assert TENANT_MODELS["aspora"]["CreateDynamoDB"] is TENANT_MODELS["vance"]["CreateDynamoDB"]


@pytest.mark.asyncio
async def test_call_validate_params_supports_vance_and_aspora():
    server = validator_server.ValidatorMCPServer()
    base_args = {
        "tool_name": "CreateS3",
        "params": json.dumps({"name": "bucketa"}),
        "user_message": "name bucketa",
        "conversation_state": {"tool_name": None, "valid": {}},
    }

    vance_response = await server._call_validate_params({**base_args, "tenant_id": "vance"})
    aspora_response = await server._call_validate_params({**base_args, "tenant_id": "aspora"})

    vance_payload = json.loads(vance_response[0].text)
    aspora_payload = json.loads(aspora_response[0].text)

    assert "error" not in vance_payload
    assert "error" not in aspora_payload
    assert vance_payload["conversation_state"]["tool_name"] == "CreateS3"
    assert aspora_payload["conversation_state"]["tool_name"] == "CreateS3"
    assert vance_payload["conversation_state"]["valid"]["name"] == "bucketa"
    assert aspora_payload["conversation_state"]["valid"]["name"] == "bucketa"


@pytest.mark.asyncio
async def test_call_validate_params_missing_tenant_id_returns_error():
    server = validator_server.ValidatorMCPServer()
    response = await server._call_validate_params({
        "tool_name": "CreateS3",
        "params": json.dumps({"name": "bucketa"}),
        "user_message": "name bucketa",
        "conversation_state": {"tool_name": None, "valid": {}},
    })

    payload = json.loads(response[0].text)
    assert "error" in payload
    assert "tenant_id" in payload["error"]


@pytest.mark.asyncio
async def test_call_validate_params_unknown_tenant_id_returns_error():
    server = validator_server.ValidatorMCPServer()
    response = await server._call_validate_params({
        "tool_name": "CreateS3",
        "params": json.dumps({"name": "bucketa"}),
        "user_message": "name bucketa",
        "tenant_id": "unknown_tenant",
        "conversation_state": {"tool_name": None, "valid": {}},
    })

    payload = json.loads(response[0].text)
    assert "error" in payload
    assert "Unknown tenant_id" in payload["error"]


@pytest.mark.asyncio
async def test_call_validate_params_returns_tool_summary_for_selected_tool():
    server = validator_server.ValidatorMCPServer()
    response = await server._call_validate_params({
        "tool_name": "CreateS3",
        "params": json.dumps({"name": "bucketa"}),
        "user_message": "name bucketa",
        "tenant_id": "vance",
        "conversation_state": {"tool_name": None, "valid": {}},
    })

    payload = json.loads(response[0].text)
    assert "tool_summary" in payload
    assert "CreateS3:" in payload["tool_summary"]
    assert "CreateSQS:" not in payload["tool_summary"]


@pytest.mark.asyncio
async def test_call_validate_params_returns_tool_summary_for_dynamodb():
    server = validator_server.ValidatorMCPServer()
    response = await server._call_validate_params({
        "tool_name": "CreateDynamoDB",
        "params": json.dumps({"identifier": "users-table"}),
        "user_message": "identifier users-table",
        "tenant_id": "vance",
        "conversation_state": {"tool_name": None, "valid": {}},
    })

    payload = json.loads(response[0].text)
    assert "tool_summary" in payload
    assert "CreateDynamoDB:" in payload["tool_summary"]
    assert "CreateS3:" not in payload["tool_summary"]


@pytest.mark.asyncio
async def test_call_validate_params_unknown_tool_for_tenant_returns_error():
    server = validator_server.ValidatorMCPServer()
    response = await server._call_validate_params({
        "tool_name": "UnknownTool",
        "params": json.dumps({"name": "bucketa"}),
        "user_message": "name bucketa",
        "tenant_id": "vance",
        "conversation_state": {"tool_name": None, "valid": {}},
    })

    payload = json.loads(response[0].text)
    assert "error" in payload
    assert "Unknown tool_name" in payload["error"]
