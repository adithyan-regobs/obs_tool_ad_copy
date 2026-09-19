"""
Unit tests for AsporaSqsScriptGenComponent (patch-in-place).

The rule under test: an existing terragrunt.hcl is the base, only the managed
fields (create_dlq, fifo_queue, timeouts/retention, cross_account_ids) may
change, and everything a human added by hand must survive a redeploy.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.plugin.aspora.script_gen_components.aspora_sqs_script_gen_component import (
    AsporaSqsScriptGenComponent,
)

GET_CONTENT_TARGET = (
    "app.plugin.aspora.script_gen_components."
    "aspora_sqs_script_gen_component.GitOpsHandler.get_content"
)
UPLOAD_TARGET = (
    "app.plugin.aspora.script_gen_components."
    "aspora_sqs_script_gen_component.FileManagerHandler.upload_file"
)

# A realistic unit: manager-written lines plus hand-added extras,
# including a multi-line cross_account_ids list.
EXISTING_WITH_EXTRAS = """terraform {
  source = "../../../../../layers//queue"
}
include "root" {
  path = find_in_parent_folders()
}
inputs = {
  identifier           = basename(get_terragrunt_dir())
  create_dlq           = true
  fifo_queue           = true
  # tuned by hand after the Nov incident
  delay_seconds        = 30
  redrive_policy_extra = { maxReceiveCount = 8 }
  cross_account_ids = [
    "111111111111",
    "222222222222"
  ]
  cross_account_access_enabled = true
  # Alarms
  create_alarm         = false
}
"""

# What any update produces from the fixture before field patches: the legacy
# cross_account_access_enabled flag (never a queue-layer variable) is renamed
# to enable_cross_account_access on every redeploy.
HEALED_EXTRAS = EXISTING_WITH_EXTRAS.replace(
    "cross_account_access_enabled", "enable_cross_account_access"
)


class TestAsporaSqsScriptGenComponent:
    @pytest.fixture
    def script_generator(self):
        return AsporaSqsScriptGenComponent()

    @pytest.fixture
    def file_location(self):
        return SimpleNamespace(
            repo="owner/repo",
            file_path="env/dev/region/sqs/my-test-queue/terragrunt.hcl",
            base_branch="main",
            feature_branch="feature/test",
            target_branch="main",
            script_gen_key="sqs",
            queue_code="queue-001",
        )

    @pytest.fixture
    def workflow_context(self):
        return SimpleNamespace(
            skip_commit=False,
            staged_files=[],
            commit_messages={},
            script_gen_responses={1: {}},
        )

    async def test_new_queue_renders_template_defaults(
        self, script_generator, file_location, workflow_context
    ):
        """A new queue (no file in the repo) renders the dev template."""
        # Arrange
        existing_file = {"exists": False}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {"identifier": "my-test-queue", "environment": "dev"},
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert
        assert "terraform {" in result
        assert "create_dlq           = true" in result
        assert "fifo_queue           = true" in result
        assert "create_alarm         = false" in result  # dev template marker
        assert "visibility_timeout_seconds" not in result
        assert "cross_account_ids" not in result

    async def test_new_queue_prod_uses_prod_template(
        self, script_generator, file_location, workflow_context
    ):
        """environment=prod picks the prod template (alarms enabled)."""
        # Arrange
        existing_file = {"exists": False}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {"identifier": "my-test-queue", "environment": "prod"},
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert
        assert "create_alarm                    = true" in result
        assert "devops_p0_alarm_sns_topic_arn" in result

    async def test_new_queue_with_optionals_and_standard_queue(
        self, script_generator, file_location, workflow_context
    ):
        """Optionals are inserted, and a standard (non-FIFO) queue also gets
        content_based_deduplication = false."""
        # Arrange
        existing_file = {"exists": False}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "identifier": "my-test-queue",
                "environment": "dev",
                "create_dlq": False,
                "fifo_queue": False,
                "visibility_timeout_seconds": 300,
                "max_receive_count": 5,
                "cross_account_ids": ["333333333333"],
            },
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert
        assert "create_dlq           = false" in result
        assert "fifo_queue           = false" in result
        assert "visibility_timeout_seconds = 300" in result
        assert "max_receive_count = 5" in result
        assert 'cross_account_ids = ["333333333333"]' in result
        assert "enable_cross_account_access = true" in result
        assert "content_based_deduplication = false" in result

    async def test_redeploy_preserves_hand_edits(
        self, script_generator, file_location, workflow_context
    ):
        """A redeploy with create_dlq=False changes ONLY that line —
        hand-added lines survive untouched."""
        # Arrange
        existing_file = {"exists": True, "content": EXISTING_WITH_EXTRAS}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "identifier": "my-test-queue",
                "environment": "dev",
                "create_dlq": False,
            },
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert: only the create_dlq value and the legacy flag rename differ
        expected = HEALED_EXTRAS.replace(
            "create_dlq           = true", "create_dlq           = false"
        )
        assert result == expected
        assert "# tuned by hand after the Nov incident" in result
        assert "delay_seconds        = 30" in result
        assert "redrive_policy_extra = { maxReceiveCount = 8 }" in result

    async def test_fields_missing_from_config_leave_lines_untouched(
        self, script_generator, file_location, workflow_context
    ):
        """A deploy config with no managed fields changes nothing — except the
        unconditional legacy-flag rename."""
        # Arrange
        existing_file = {"exists": True, "content": EXISTING_WITH_EXTRAS}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {"identifier": "my-test-queue", "environment": "dev"},
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert
        assert result == HEALED_EXTRAS

    async def test_legacy_flag_renamed_even_when_cross_account_untouched(
        self, script_generator, file_location, workflow_context
    ):
        """A redeploy that only touches an unrelated field still renames the
        legacy cross_account_access_enabled line, keeping its value."""
        # Arrange
        existing_file = {"exists": True, "content": EXISTING_WITH_EXTRAS}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "identifier": "my-test-queue",
                "environment": "dev",
                "visibility_timeout_seconds": 45,
            },
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert
        assert "cross_account_access_enabled" not in result
        assert "enable_cross_account_access = true" in result
        assert "visibility_timeout_seconds = 45" in result
        # untouched neighbours survive
        assert "111111111111" in result
        assert "# tuned by hand after the Nov incident" in result

    async def test_multiline_cross_account_list_is_replaced_cleanly(
        self, script_generator, file_location, workflow_context
    ):
        """A hand-written multi-line cross_account_ids list is replaced as one
        unit — no stray brackets, other lines untouched."""
        # Arrange
        existing_file = {"exists": True, "content": EXISTING_WITH_EXTRAS}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "identifier": "my-test-queue",
                "environment": "dev",
                "cross_account_ids": ["999999999999"],
            },
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert
        assert 'cross_account_ids = ["999999999999"]' in result
        assert "111111111111" not in result
        assert "222222222222" not in result
        assert result.count("]") == result.count("[")  # brackets stayed balanced
        assert "enable_cross_account_access = true" in result
        assert "# tuned by hand after the Nov incident" in result

    async def test_clearing_cross_account_patches_lines_not_deletes(
        self, script_generator, file_location, workflow_context
    ):
        """cross_account_ids=[] empties the list and sets enabled=false —
        the lines themselves are never removed."""
        # Arrange
        existing_file = {"exists": True, "content": EXISTING_WITH_EXTRAS}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "identifier": "my-test-queue",
                "environment": "dev",
                "cross_account_ids": [],
            },
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert: 4-line list collapsed to one line, nothing else removed
        assert "cross_account_ids = []" in result
        assert "enable_cross_account_access = false" in result
        assert len(result.splitlines()) == len(EXISTING_WITH_EXTRAS.splitlines()) - 3
        assert "delay_seconds        = 30" in result

    async def test_clearing_cross_account_does_not_insert_missing_lines(
        self, script_generator, file_location, workflow_context
    ):
        """cross_account_ids=[] on a file without those lines adds nothing."""
        # Arrange
        existing = (
            "inputs = {\n"
            "  identifier           = basename(get_terragrunt_dir())\n"
            "  create_dlq           = true\n"
            "  fifo_queue           = true\n"
            "}\n"
        )
        existing_file = {"exists": True, "content": existing}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "identifier": "my-test-queue",
                "environment": "dev",
                "cross_account_ids": [],
            },
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert
        assert result == existing

    async def test_missing_optional_is_inserted_after_anchor(
        self, script_generator, file_location, workflow_context
    ):
        """visibility_timeout not in the file → inserted right after fifo_queue."""
        # Arrange
        existing_file = {"exists": True, "content": EXISTING_WITH_EXTRAS}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "identifier": "my-test-queue",
                "environment": "dev",
                "visibility_timeout_seconds": 120,
            },
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert
        assert "  fifo_queue           = true\n  visibility_timeout_seconds = 120" in result
        assert "# tuned by hand after the Nov incident" in result

    async def test_fifo_turned_off_adds_content_based_dedup(
        self, script_generator, file_location, workflow_context
    ):
        """Switching a queue to standard (fifo=false) also writes
        content_based_deduplication = false."""
        # Arrange
        existing_file = {"exists": True, "content": EXISTING_WITH_EXTRAS}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "identifier": "my-test-queue",
                "environment": "dev",
                "fifo_queue": False,
            },
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert
        assert "fifo_queue           = false" in result
        assert "content_based_deduplication = false" in result

    async def test_boolean_parameters_arrive_as_strings(
        self, script_generator, file_location, workflow_context
    ):
        """The FE sends form values as strings ('false', not False) —
        they must be coerced, not treated as truthy."""
        # Arrange
        existing_file = {"exists": False}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "identifier": "my-test-queue",
                "environment": "dev",
                "create_dlq": "false",
                "fifo_queue": "true",
            },
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert
        assert "create_dlq           = false" in result
        assert "fifo_queue           = true" in result
        assert "content_based_deduplication" not in result

    async def test_environment_is_case_insensitive(
        self, script_generator, file_location, workflow_context
    ):
        """environment='PROD' still picks the prod template."""
        # Arrange
        existing_file = {"exists": False}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {"identifier": "my-test-queue", "environment": "PROD"},
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert
        assert "devops_p0_alarm_sns_topic_arn" in result

    async def test_alias_keys_are_accepted(
        self, script_generator, file_location, workflow_context
    ):
        """Older payloads use alias keys (dlq, fifo, *_retention_seconds) —
        they map onto the real fields."""
        # Arrange
        existing_file = {"exists": False}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "identifier": "my-test-queue",
                "environment": "dev",
                "dlq": False,
                "fifo": False,
                "main_queue_retention_seconds": 3600,
                "dlq_retention_seconds": 7200,
            },
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert
        assert "create_dlq           = false" in result
        assert "fifo_queue           = false" in result
        assert "message_retention_seconds = 3600" in result
        assert "dlq_message_retention_seconds = 7200" in result
        assert "content_based_deduplication = false" in result

    async def test_empty_cross_account_ids_on_new_queue_adds_no_lines(
        self, script_generator, file_location, workflow_context
    ):
        """An empty cross_account_ids on a NEW queue must not write the lines."""
        # Arrange
        existing_file = {"exists": False}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "identifier": "my-test-queue",
                "environment": "dev",
                "cross_account_ids": [],
            },
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert
        assert "cross_account_ids" not in result
        assert "enable_cross_account_access" not in result

    async def test_null_clears_field_by_removing_its_line(
        self, script_generator, file_location, workflow_context
    ):
        """Explicit null (user emptied a field that had a value) removes the
        line so the terraform module default applies."""
        # Arrange: file carries a visibility override
        existing = EXISTING_WITH_EXTRAS.replace(
            "  fifo_queue           = true",
            "  fifo_queue           = true\n  visibility_timeout_seconds = 3000",
        )
        existing_file = {"exists": True, "content": existing}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "identifier": "my-test-queue",
                "environment": "dev",
                "visibility_timeout_seconds": None,
            },
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert: only the visibility line is gone (plus the legacy rename)
        assert result == HEALED_EXTRAS
        assert "visibility_timeout_seconds" not in result

    async def test_null_on_field_not_in_file_is_a_noop(
        self, script_generator, file_location, workflow_context
    ):
        """Clearing a field the file never had changes nothing."""
        # Arrange
        existing_file = {"exists": True, "content": EXISTING_WITH_EXTRAS}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "identifier": "my-test-queue",
                "environment": "dev",
                "max_receive_count": None,
            },
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert
        assert result == HEALED_EXTRAS

    async def test_null_cross_account_removes_list_and_flag_lines(
        self, script_generator, file_location, workflow_context
    ):
        """Null cross_account_ids removes the (multi-line) list AND the
        cross_account_access_enabled line; everything else stays."""
        # Arrange
        existing_file = {"exists": True, "content": EXISTING_WITH_EXTRAS}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "identifier": "my-test-queue",
                "environment": "dev",
                "cross_account_ids": None,
            },
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert
        assert "cross_account_ids" not in result
        assert "enable_cross_account_access" not in result
        assert result.count("[") == result.count("]")
        assert "delay_seconds        = 30" in result
        assert "# tuned by hand after the Nov incident" in result

    async def test_null_checkbox_leaves_template_field_untouched(
        self, script_generator, file_location, workflow_context
    ):
        """create_dlq/fifo_queue always exist in the template — null must not
        flip them to false or remove them."""
        # Arrange
        existing_file = {"exists": True, "content": EXISTING_WITH_EXTRAS}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "identifier": "my-test-queue",
                "environment": "dev",
                "create_dlq": None,
                "fifo_queue": None,
            },
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert
        assert result == HEALED_EXTRAS
        assert "create_dlq           = true" in result

    async def test_upload_writes_artifact_keys_and_db(
        self, script_generator, file_location, workflow_context
    ):
        """upload_to_s3=True uploads original + preview and records the
        artifact keys against the queue item in the DB."""
        # Arrange
        existing_file = {"exists": False}
        repository = SimpleNamespace(update_artifact_s3_key=AsyncMock())
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {"identifier": "my-test-queue", "environment": "dev"},
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)), \
             patch(UPLOAD_TARGET, new=AsyncMock(return_value={"location": "s3://b/k"})) as mock_upload:
            await script_generator.generate(
                tenant="aspora",
                repository=repository,
                file_location=file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=True,
                db=None,
            )

        # Assert
        uploaded_keys = [call.kwargs["key"] for call in mock_upload.call_args_list]
        assert uploaded_keys == ["sqs/my-test-queue.hcl", "preview/sqs/my-test-queue.hcl"]
        repository.update_artifact_s3_key.assert_awaited_once()
        assert repository.update_artifact_s3_key.await_args.args[0] == "queue-001"

    async def test_preview_upload_failure_is_non_blocking(
        self, script_generator, file_location, workflow_context
    ):
        """The preview upload failing must not fail the generation —
        only the original upload is mandatory."""
        # Arrange
        existing_file = {"exists": False}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {"identifier": "my-test-queue", "environment": "dev"},
        }

        upload_calls = {"count": 0}

        async def upload_fails_on_preview(**kwargs):
            upload_calls["count"] += 1
            if upload_calls["count"] == 2:  # second call = preview upload
                raise RuntimeError("preview upload down")
            return {"location": "s3://b/k"}

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)), \
             patch(UPLOAD_TARGET, new=upload_fails_on_preview):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=True,
                db=None,
            )

        # Assert: generation still completed
        assert "create_dlq" in result

    async def test_second_generate_patches_staged_content_not_repo(
        self, script_generator, file_location, workflow_context
    ):
        """Two deploys in one run: the second builds on the first's staged
        content (no second git fetch), so both patches end up in the file."""
        # Arrange
        existing_file = {"exists": True, "content": EXISTING_WITH_EXTRAS}
        first_queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "identifier": "my-test-queue",
                "environment": "dev",
                "create_dlq": False,
            },
        }
        second_queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "identifier": "my-test-queue",
                "environment": "dev",
                "max_receive_count": 4,
            },
        }

        # Act: first deploy fetches from git
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)) as mock_get:
            first = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=file_location,
                queue_dict=first_queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )
        assert mock_get.await_count == 1
        assert "create_dlq           = false" in first

        # Act: second deploy in the same workflow run
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)) as mock_get:
            second = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=file_location,
                queue_dict=second_queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert: staged cache was used, both patches present
        assert mock_get.await_count == 0
        assert "create_dlq           = false" in second
        assert "max_receive_count = 4" in second

    async def test_result_is_staged_for_commit_with_message(
        self, script_generator, file_location, workflow_context
    ):
        """The generated content is staged for the git commit, stored in the
        workflow responses, and a commit message line is recorded."""
        # Arrange
        existing_file = {"exists": False}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {"identifier": "my-test-queue", "environment": "dev"},
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert
        assert workflow_context.script_gen_responses[1]["sqs"]["original_content"] == result
        assert len(workflow_context.staged_files) == 1
        assert workflow_context.staged_files[0]["content"] == result
        assert "queue-001" in workflow_context.commit_messages["owner/repo|||main"]
