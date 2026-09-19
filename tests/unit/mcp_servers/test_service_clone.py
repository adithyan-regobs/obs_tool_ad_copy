"""clone_service — a new service seeded with another service's settings."""

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.mcp_servers.devlift_mcp import dispatcher
from app.mcp_servers.devlift_mcp.obs_tool_client import ObsToolAPIError
from tests.unit.mcp_servers.test_service_edit import Harness as EditHarness, SC


class Harness(EditHarness):
    """The edit harness (source lookup, DB names, chatbot session) plus the
    'does the new name already exist?' lookup."""

    def __init__(self, *, existing_new_name=None, **kw):
        super().__init__(**kw)
        self.existing_new_name = existing_new_name

    def patches(self):
        base = [p for p in super().patches() if getattr(p, "attribute", None) != "ServicesMstRepository"]
        services_repo = MagicMock()
        services_repo.find_by_name_for_mcp = AsyncMock(
            side_effect=lambda *, tenant_code, service_name: (
                SimpleNamespace(code="svc-dup", name=service_name)
                if service_name == self.existing_new_name else None
            )
        )
        base.append(patch.object(dispatcher, "ServicesMstRepository", return_value=services_repo))
        return base

    async def run(self, **kwargs):
        with ExitStack() as stack:
            for p in self.patches():
                stack.enter_context(p)
            return await dispatcher.start_service_clone_handler(**kwargs)


@pytest.mark.asyncio
async def test_clone_prefills_source_settings_under_new_name():
    h = Harness()
    result = await h.run(source_service_name="ad-test-9999", new_service_name="ad-test-clone")

    assert result["status"] == "success"
    assert result["source_service_name"] == "ad-test-9999"
    assert result["new_service_name"] == "ad-test-clone"
    assert result["source_service_config_code"] == SC
    assert result["placement"] == "Stage / Mumbai"
    assert result["overrides"] == {}
    assert result["next_action"]["type"] == "continue_chat"
    assert result["ticket_code"] in result["next_action"]["instruction"]

    prefill = h.post_select_form.await_args.kwargs["prefill"]
    assert prefill["service_name"] == "ad-test-clone"                    # swapped
    assert prefill["repository"] == "Vance-Club/devops-sandbox-golang"  # copied as-is
    assert prefill["cpu_requested"] == "1"
    assert prefill["custom_iam_policies"] == ["s3", "sqs", "dynamodb", "ses"]
    assert prefill["environment"] == "Stage" and prefill["geo_location"] == "Mumbai"
    assert h.post_select_form.await_args.kwargs["form_id"] == "eks_service_form"


@pytest.mark.asyncio
async def test_clone_into_other_environment_and_geo():
    h = Harness()
    result = await h.run(
        source_service_name="ad-test-9999", new_service_name="ad-test-qa",
        environment="qa", geo_location="Mumbai",
    )
    assert result["status"] == "success"
    assert result["overrides"] == {"environment": "QA", "geo_location": "Mumbai"}
    prefill = h.post_select_form.await_args.kwargs["prefill"]
    assert prefill["environment"] == "QA"
    assert prefill["geo_location"] == "Mumbai"
    assert prefill["service_name"] == "ad-test-qa"


@pytest.mark.asyncio
async def test_clone_refuses_existing_target_name():
    h = Harness(existing_new_name="ad-test-copy")
    result = await h.run(source_service_name="ad-test-9999", new_service_name="ad-test-copy")
    assert result["status"] == "error"
    assert result["reason"] == "already_exists"
    h.post_select_form.assert_not_awaited()
    h.get_service_config.assert_not_awaited()


@pytest.mark.asyncio
async def test_clone_validates_new_name():
    h = Harness()
    result = await h.run(source_service_name="ad-test-9999", new_service_name="Bad_Name")
    assert result["reason"] == "invalid_name"
    result = await h.run(source_service_name="ad-test-9999", new_service_name="")
    assert result["reason"] == "missing_target"


@pytest.mark.asyncio
async def test_clone_needs_a_source():
    h = Harness()
    result = await h.run(new_service_name="ad-test-clone")
    assert result["reason"] == "missing_target"
    assert "copied from" in result["message"]


@pytest.mark.asyncio
async def test_clone_source_not_viewable_is_permission_error():
    h = Harness()
    h.get_service_config.side_effect = ObsToolAPIError(403, "denied", "/service-configs/by-code/x")
    result = await h.run(source_service_name="ad-test-9999", new_service_name="ad-test-clone")
    assert result["status"] == "error"
    assert result["reason"] == "permission_denied"
    h.post_select_form.assert_not_awaited()
