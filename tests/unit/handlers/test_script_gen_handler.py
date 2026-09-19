import pytest
from types import SimpleNamespace
from unittest.mock import patch

from app.handlers.script_gen_handler import ScriptGenHandler
from app.schemas.pr_workflow_context import PRWorkflowContext


class TestScriptGenHandler:
    @pytest.mark.asyncio
    async def test_generate_script_awaits_async_generator(self):
        class AsyncGenerator:
            async def generate(self, **kwargs):
                return "async-content"

        file_location = SimpleNamespace(file_path="path.hcl", script_gen_key="ecs")
        workflow_context = PRWorkflowContext()

        with patch.object(ScriptGenHandler, "get_script_generator", return_value=AsyncGenerator()):
            result = await ScriptGenHandler.generate_script(
                tenant="aspora",
                queue_id=1,
                config_snapshot={},
                file_location=file_location,
                workflow_context=workflow_context
            )

        assert result["script_content"] == "async-content"

    @pytest.mark.asyncio
    async def test_generate_script_handles_sync_generator(self):
        class SyncGenerator:
            def generate(self, **kwargs):
                return "sync-content"

        file_location = SimpleNamespace(file_path="path.hcl", script_gen_key="s3")
        workflow_context = PRWorkflowContext()

        with patch.object(ScriptGenHandler, "get_script_generator", return_value=SyncGenerator()):
            result = await ScriptGenHandler.generate_script(
                tenant="aspora",
                queue_id=1,
                config_snapshot={},
                file_location=file_location,
                workflow_context=workflow_context
            )

        assert result["script_content"] == "sync-content"
