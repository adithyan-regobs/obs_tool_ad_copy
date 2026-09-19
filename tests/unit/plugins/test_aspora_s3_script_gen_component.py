"""
Unit tests for AsporaS3ScriptGenComponent (patch-in-place).

The rule under test: an existing terragrunt.hcl is the base, only the managed
fields (versioning, enable_s3_replication, cross_account_account_id) may
change, and everything a human added by hand must survive a redeploy.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.plugin.aspora.script_gen_components.aspora_s3_script_gen_component import (
    AsporaS3ScriptGenComponent,
)

GET_CONTENT_TARGET = (
    "app.plugin.aspora.script_gen_components."
    "aspora_s3_script_gen_component.GitOpsHandler.get_content"
)

# A realistic unit: manager-written lines plus hand-added extras.
EXISTING_WITH_EXTRAS = """terraform {
  source = "../../../../../layers/bucket"
}
include "root" {
  path = find_in_parent_folders()
}
inputs = {
  identifier            = basename(get_terragrunt_dir())
  versioning            = false
  # keep objects on delete — added by platform team
  force_destroy         = false
  lifecycle_rules = [
    {
      id         = "expire-tmp"
      enabled    = true
      expiration = { days = 30 }
    }
  ]
}
"""


class TestAsporaS3ScriptGenComponent:
    @pytest.fixture
    def script_generator(self):
        return AsporaS3ScriptGenComponent()

    @pytest.fixture
    def file_location(self):
        return SimpleNamespace(
            repo="owner/repo",
            file_path="env/dev/region/s3/my-test-bucket/terragrunt.hcl",
            base_branch="main",
            feature_branch="feature/test",
            target_branch="main",
            script_gen_key="s3",
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

    async def test_new_bucket_renders_template_defaults(
        self, script_generator, file_location, workflow_context
    ):
        """A new bucket (no file in the repo) renders the plain template."""
        # Arrange
        existing_file = {"exists": False}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {"identifier": "my-test-bucket"},
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
        assert "versioning            = false" in result
        assert "enable_s3_replication" not in result
        assert "cross_account_account_id" not in result

    async def test_new_bucket_with_all_features_enabled(
        self, script_generator, file_location, workflow_context
    ):
        """Versioning, replication and cross-account land in the right order."""
        # Arrange
        existing_file = {"exists": False}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "identifier": "my-test-bucket",
                "versioning": True,
                "enable_s3_replication": True,
                "cross_account_id": "123456789012",
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
        assert "versioning            = true" in result
        assert "  enable_s3_replication = true" in result
        assert '  cross_account_account_id = "123456789012"' in result
        assert result.index("enable_s3_replication") < result.index("cross_account_account_id")

    async def test_redeploy_preserves_hand_edits(
        self, script_generator, file_location, workflow_context
    ):
        """A redeploy that flips versioning changes ONLY that line —
        hand-added lines (comments, force_destroy, lifecycle_rules) survive."""
        # Arrange
        existing_file = {"exists": True, "content": EXISTING_WITH_EXTRAS}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {"identifier": "my-test-bucket", "versioning": True},
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

        # Assert: the only difference is the versioning value
        expected = EXISTING_WITH_EXTRAS.replace(
            "versioning            = false", "versioning            = true"
        )
        assert result == expected
        assert "# keep objects on delete — added by platform team" in result
        assert 'id         = "expire-tmp"' in result

    async def test_field_missing_from_config_leaves_line_untouched(
        self, script_generator, file_location, workflow_context
    ):
        """No versioning key in the deploy config → a hand-set
        versioning=true in the repo stays true (no reset to template default)."""
        # Arrange
        hand_set_true = EXISTING_WITH_EXTRAS.replace(
            "versioning            = false", "versioning            = true"
        )
        existing_file = {"exists": True, "content": hand_set_true}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {"identifier": "my-test-bucket"},
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
        assert result == hand_set_true

    async def test_turning_feature_off_patches_line_not_deletes_it(
        self, script_generator, file_location, workflow_context
    ):
        """Replication turned off → the line becomes false; it is never removed."""
        # Arrange
        with_replication = EXISTING_WITH_EXTRAS.replace(
            "  versioning            = false",
            "  versioning            = false\n  enable_s3_replication = true",
        )
        existing_file = {"exists": True, "content": with_replication}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "identifier": "my-test-bucket",
                "enable_s3_replication": False,
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

        # Assert: same number of lines, value patched
        assert "enable_s3_replication = false" in result
        assert len(result.splitlines()) == len(with_replication.splitlines())

    async def test_turning_off_feature_not_in_file_adds_nothing(
        self, script_generator, file_location, workflow_context
    ):
        """Replication off + no replication line in the file → file unchanged
        (a false line is never inserted)."""
        # Arrange
        existing_file = {"exists": True, "content": EXISTING_WITH_EXTRAS}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "identifier": "my-test-bucket",
                "enable_s3_replication": False,
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
        assert result == EXISTING_WITH_EXTRAS

    async def test_enabling_missing_feature_inserts_after_anchor(
        self, script_generator, file_location, workflow_context
    ):
        """Replication enabled but the line is missing → inserted right after
        the versioning line, matching its indentation."""
        # Arrange
        existing_file = {"exists": True, "content": EXISTING_WITH_EXTRAS}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "identifier": "my-test-bucket",
                "enable_s3_replication": True,
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
        assert "  versioning            = false\n  enable_s3_replication = true" in result
        assert "lifecycle_rules" in result

    async def test_null_replication_removes_its_line(
        self, script_generator, file_location, workflow_context
    ):
        """Explicit null (user cleared the field) removes the replication line
        so the terraform module default applies."""
        # Arrange: file carries a replication override
        with_replication = EXISTING_WITH_EXTRAS.replace(
            "  versioning            = false",
            "  versioning            = false\n  enable_s3_replication = true",
        )
        existing_file = {"exists": True, "content": with_replication}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "identifier": "my-test-bucket",
                "enable_s3_replication": None,
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

        # Assert: back to the file without the line
        assert result == EXISTING_WITH_EXTRAS
        assert "enable_s3_replication" not in result

    async def test_null_versioning_leaves_template_field_untouched(
        self, script_generator, file_location, workflow_context
    ):
        """versioning always exists in the template — null must not flip it
        to false or remove it."""
        # Arrange
        hand_set_true = EXISTING_WITH_EXTRAS.replace(
            "versioning            = false", "versioning            = true"
        )
        existing_file = {"exists": True, "content": hand_set_true}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {"identifier": "my-test-bucket", "versioning": None},
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
        assert result == hand_set_true

    async def test_null_cross_account_removes_its_line(
        self, script_generator, file_location, workflow_context
    ):
        """Explicit null on cross_account_id removes the cross-account line."""
        # Arrange
        with_cross_account = EXISTING_WITH_EXTRAS.replace(
            "  versioning            = false",
            '  versioning            = false\n  cross_account_account_id = "123456789012"',
        )
        existing_file = {"exists": True, "content": with_cross_account}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "identifier": "my-test-bucket",
                "cross_account_id": None,
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
        assert result == EXISTING_WITH_EXTRAS
        assert "cross_account_account_id" not in result

    async def test_second_generate_patches_staged_content_not_repo(
        self, script_generator, file_location, workflow_context
    ):
        """Two deploys in one run: the second must build on the first's staged
        content (no second git fetch), so both patches end up in the file."""
        # Arrange
        existing_file = {"exists": True, "content": EXISTING_WITH_EXTRAS}
        first_queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {"identifier": "my-test-bucket", "versioning": True},
        }
        second_queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "identifier": "my-test-bucket",
                "enable_s3_replication": True,
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
        assert "versioning            = true" in first

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
        assert "versioning            = true" in second
        assert "enable_s3_replication = true" in second

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
            "config_snapshot": {"identifier": "my-test-bucket", "versioning": True},
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
        assert workflow_context.script_gen_responses[1]["s3"]["original_content"] == result
        assert len(workflow_context.staged_files) == 1
        assert workflow_context.staged_files[0]["file_path"] == file_location.file_path
        assert workflow_context.staged_files[0]["content"] == result
        assert "queue-001" in workflow_context.commit_messages["owner/repo|||main"]
