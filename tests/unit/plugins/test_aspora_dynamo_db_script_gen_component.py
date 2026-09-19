"""
Unit tests for AsporaDynamoDbScriptgenComponent (patch-in-place).

The rule under test: an existing terragrunt.hcl is the base, only the
partition_key line and ITS entry in `attributes = [...]` may change —
other attribute entries (range keys, GSI keys) and hand-added inputs
must survive a redeploy.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.plugin.aspora.script_gen_components.aspora_dynamo_db_script_gen_component import (
    AsporaDynamoDbScriptgenComponent,
)

GET_CONTENT_TARGET = (
    "app.plugin.aspora.script_gen_components."
    "aspora_dynamo_db_script_gen_component.GitOpsHandler.get_content"
)

# A realistic unit: partition key + hand-added range key, second attribute
# entry, and a GSI block.
EXISTING_WITH_EXTRAS = """terraform {
  source = "../../../../../layers/dynamo"
}
include "root" {
  path = find_in_parent_folders()
}
inputs = {
  identifier            = basename(get_terragrunt_dir())
  partition_key         = "user_id"
  range_key             = "created_at"
  # attributes must cover the GSI keys too — added by hand
  attributes            = [
                            {
                              name = "user_id"
                              type = "S"
                            },
                            {
                              name = "created_at"
                              type = "N"
                            }
                          ]
  global_secondary_indexes = [
    {
      name      = "by-created"
      hash_key  = "created_at"
    }
  ]
}
"""


class TestAsporaDynamoDbScriptgenComponent:
    @pytest.fixture
    def script_generator(self):
        return AsporaDynamoDbScriptgenComponent()

    @pytest.fixture
    def file_location(self):
        return SimpleNamespace(
            repo="owner/repo",
            file_path="env/dev/region/dynamo/my-test-table/terragrunt.hcl",
            base_branch="main",
            feature_branch="feature/test",
            target_branch="main",
            script_gen_key="dynamodb",
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

    @pytest.mark.parametrize("key_type", ["S", "N", "B"])
    async def test_new_table_renders_template_with_key_type(
        self, script_generator, file_location, workflow_context, key_type
    ):
        """A new table renders the template with the given partition key and
        type; the template placeholder is fully replaced."""
        # Arrange
        existing_file = {"exists": False}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "identifier": "my-test-table",
                "partition_key": "order_id",
                "partition_key_type": key_type,
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
        assert 'partition_key         = "order_id"' in result
        assert 'name = "order_id"' in result
        assert f'type = "{key_type}"' in result
        assert "event_name" not in result  # template placeholder gone

    async def test_new_table_defaults_key_type_to_string(
        self, script_generator, file_location, workflow_context
    ):
        """No partition_key_type given → defaults to S."""
        # Arrange
        existing_file = {"exists": False}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {"identifier": "my-test-table", "partition_key": "pk"},
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
        assert 'partition_key         = "pk"' in result
        assert 'type = "S"' in result

    async def test_special_characters_in_key_are_escaped(
        self, script_generator, file_location, workflow_context
    ):
        """A quote or ${...} in the key name must not break out of the HCL
        string it lands in."""
        # Arrange
        existing_file = {"exists": False}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "identifier": "my-test-table",
                "partition_key": 'id"x${oops}',
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

        # Assert: raw quote and interpolation cannot survive unescaped
        assert 'id"x${oops}"' not in result.replace('\\"', "")
        assert "${oops}" not in result or "$${oops}" in result

    async def test_redeploy_renames_key_and_preserves_other_attributes(
        self, script_generator, file_location, workflow_context
    ):
        """Changing the partition key updates its line and ITS attributes
        entry — the range key attribute and the GSI block survive."""
        # Arrange
        existing_file = {"exists": True, "content": EXISTING_WITH_EXTRAS}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "identifier": "my-test-table",
                "partition_key": "account_id",
                "partition_key_type": "S",
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

        # Assert: key renamed in place, not duplicated
        assert 'partition_key         = "account_id"' in result
        assert 'name = "account_id"' in result
        assert 'name = "user_id"' not in result
        # hand-added pieces survive
        assert 'name = "created_at"' in result
        assert 'type = "N"' in result
        assert 'range_key             = "created_at"' in result
        assert "# attributes must cover the GSI keys too — added by hand" in result
        assert 'name      = "by-created"' in result

    async def test_redeploy_same_key_new_type_patches_entry_only(
        self, script_generator, file_location, workflow_context
    ):
        """Same key, new type → only that entry's type line changes;
        the rest of the file is byte-identical."""
        # Arrange
        existing_file = {"exists": True, "content": EXISTING_WITH_EXTRAS}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "identifier": "my-test-table",
                "partition_key": "user_id",
                "partition_key_type": "B",
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
        expected = EXISTING_WITH_EXTRAS.replace(
            'name = "user_id"\n                              type = "S"',
            'name = "user_id"\n                              type = "B"',
        )
        assert result == expected

    async def test_no_partition_key_in_config_leaves_file_untouched(
        self, script_generator, file_location, workflow_context
    ):
        """A deploy config without partition_key changes nothing at all."""
        # Arrange
        existing_file = {"exists": True, "content": EXISTING_WITH_EXTRAS}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {"identifier": "my-test-table"},
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
        assert result == EXISTING_WITH_EXTRAS

    async def test_missing_attributes_entry_is_appended_not_replacing_others(
        self, script_generator, file_location, workflow_context
    ):
        """If the new key has no entry in attributes, one is appended (with a
        separating comma) and the existing entry is kept."""
        # Arrange: file whose attributes hold only the created_at entry
        existing = EXISTING_WITH_EXTRAS.replace(
            'partition_key         = "user_id"', 'partition_key         = "pk"'
        )
        existing = existing.replace(
            "                            {\n"
            '                              name = "user_id"\n'
            '                              type = "S"\n'
            "                            },\n",
            "",
        )
        existing_file = {"exists": True, "content": existing}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "identifier": "my-test-table",
                "partition_key": "tenant_id",
                "partition_key_type": "S",
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
        assert 'name = "tenant_id"' in result
        assert 'name = "created_at"' in result
        assert result.count("[") == result.count("]")  # brackets stayed balanced
        assert "},\n" in result  # comma separates the kept and appended entries

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
                "identifier": "my-test-table",
                "partition_key": "account_id",
            },
        }
        second_queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "identifier": "my-test-table",
                "partition_key": "account_id",
                "partition_key_type": "B",
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
        assert 'partition_key         = "account_id"' in first

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
        assert 'name = "account_id"' in second
        assert 'type = "B"' in second

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
            "config_snapshot": {"identifier": "my-test-table", "partition_key": "pk"},
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
        assert workflow_context.script_gen_responses[1]["dynamodb"]["original_content"] == result
        assert len(workflow_context.staged_files) == 1
        assert workflow_context.staged_files[0]["content"] == result
        assert "queue-001" in workflow_context.commit_messages["owner/repo|||main"]
