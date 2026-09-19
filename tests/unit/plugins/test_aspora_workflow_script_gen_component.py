"""
Unit tests for AsporaWorkflowScriptGenComponent (patch-in-place).

The rule under test: an existing deploy workflow YAML is the base and only
the managed scalar lines (versions, cluster, ECR repo, role, region, trigger
branch) may change — hand-added steps, env vars, and structural blocks
(paths filter, build commands) must survive a redeploy.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.plugin.aspora.script_gen_components.aspora_workflow_script_gen_component import (
    AsporaWorkflowScriptGenComponent,
)

MODULE = "app.plugin.aspora.script_gen_components.aspora_workflow_script_gen_component"
GET_CONTENT_TARGET = f"{MODULE}.GitOpsHandler.get_content"

# A rendered-style ECS workflow with hand edits sprinkled in.
ECS_EXISTING = """name: Deploy my-svc to dev
on:
  push:
    branches:
      - main
    paths:
      - 'services/my-svc/**'   # hand-tuned monorepo filter

  workflow_dispatch:

permissions:
  id-token: write
  contents: read

env:
  AWS_REGION: ap-south-1
  ECR_REPOSITORY: 878097483768.dkr.ecr.ap-south-1.amazonaws.com/my-svc
  SERVICE_NAME: my_svc-dev
  ENVIRONMENT: dev
  ECS_CLUSTER: core-stage-cluster
  EXTRA_FLAG: "added-by-hand"

jobs:
  build-and-deploy:
    runs-on: [self-hosted, aws]
    steps:
      - name: Checkout code
        uses: actions/checkout@v4

      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v5.1.0
        with:
          role-to-assume: arn:aws:iam::878097483768:role/deploy
          aws-region: ${{ env.AWS_REGION }}

      - name: Set up Go
        uses: actions/setup-go@v5
        with:
          go-version: '1.22'

      # hand-added cache step — must survive redeploys
      - name: Cache modules
        uses: actions/cache@v4
        with:
          path: ~/go/pkg/mod
          key: go-mod

      - name: Build Go binary
        run: CGO_ENABLED=0 go build -o app ./cmd/custom
"""

# A rendered-style EKS reusable-workflow call with hand edits.
EKS_EXISTING = """name: deploy my-svc-service to stage backend eks

on:
  push:
    branches:
      - main
      - hotfix/manual    # added by hand
  workflow_dispatch:

jobs:
  build:
    name: Build, Push, and Notify
    uses: Vance-Club/shared-lib/.github/workflows/build.yaml@main
    with:
      service_name:        my-svc-service
      organization:        core
      environment:         stage
      index:               "01"
      aws_region:          ap-south-1
      language:            golang
      go_version:          "1.22"
      golang_binary_name:  app
      golang_build_path:   ./cmd
      dockerfile_path:     Dockerfile
      timeout_minutes:     30   # added by hand
    secrets: inherit
"""


class TestAsporaWorkflowScriptGenComponent:
    @pytest.fixture
    def script_generator(self):
        return AsporaWorkflowScriptGenComponent()

    @pytest.fixture
    def ecs_file_location(self):
        return SimpleNamespace(
            repo="owner/app-repo",
            file_path=".github/workflows/deploy-my-svc-dev.yml",
            base_branch="main",
            feature_branch="feature/test",
            target_branch="main",
            script_gen_key="ecs_pipeline",
            queue_code="queue-001",
            config=None,
        )

    @pytest.fixture
    def eks_file_location(self):
        return SimpleNamespace(
            repo="owner/app-repo",
            file_path=".github/workflows/deploy-my-svc-service.yml",
            base_branch="main",
            feature_branch="feature/test",
            target_branch="main",
            script_gen_key="eks_pipeline_workflow",
            queue_code="queue-001",
            config=None,
        )

    @pytest.fixture
    def workflow_context(self):
        return SimpleNamespace(
            skip_commit=False,
            staged_files=[],
            commit_messages={},
            script_gen_responses={1: {"ecs_pipeline": {}, "eks_pipeline_workflow": {}}},
        )

    async def test_new_ecs_workflow_renders_go_template(
        self, script_generator, ecs_file_location, workflow_context
    ):
        """A new ECS service renders the go template with its values filled in."""
        # Arrange
        existing_file = {"exists": False}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "infrastructure_type": "ecs",
                "service_name": "my-svc",
                "environment": "dev",
                "language_ref_code": "GO_1_23",
                "go_version": "1.23",
                "ecr_repository": "878097483768.dkr.ecr.ap-south-1.amazonaws.com/my-svc",
                "ecs_cluster": "core-stage-cluster",
                "aws_role_arn": "arn:aws:iam::878097483768:role/deploy",
                "aws_region": "ap-south-1",
            },
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=ecs_file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert
        assert "name: Deploy my-svc to dev" in result
        assert "ECR_REPOSITORY: 878097483768.dkr.ecr.ap-south-1.amazonaws.com/my-svc" in result
        assert "ECS_CLUSTER: core-stage-cluster" in result
        assert "role-to-assume: arn:aws:iam::878097483768:role/deploy" in result
        assert "go-version: '1.23'" in result
        assert "{{" not in result.replace("${{", "")  # no unfilled placeholders

    async def test_new_ecs_workflow_java_ref_picks_gradle_template(
        self, script_generator, ecs_file_location, workflow_context
    ):
        """language_ref_code JAVA_21 (no MAVEN) selects the gradle template."""
        # Arrange
        existing_file = {"exists": False}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "infrastructure_type": "ecs",
                "service_name": "my-svc",
                "environment": "dev",
                "language_ref_code": "JAVA_21",
                "java_version": "21",
            },
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=ecs_file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert
        assert "gradle" in result.lower()
        assert "java-version: 21" in result  # java templates render unquoted

    async def test_new_eks_workflow_renders_shared_lib_call(
        self, script_generator, eks_file_location, workflow_context
    ):
        """A new EKS service renders the reusable-workflow call template."""
        # Arrange
        existing_file = {"exists": False}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "infrastructure_type": "eks",
                "service_name": "my-svc",
                "environment": "staging",
                "language": "golang",
                "org_name": "core",
                "aws_region": "ap-south-1",
                "go_version": "1.23",
            },
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=eks_file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert
        assert "uses: Vance-Club/shared-lib/.github/workflows/build.yaml@main" in result
        assert "service_name:        my-svc-service" in result
        assert "environment:         stage" in result
        assert 'go_version:          "1.23"' in result
        assert "skip_slack:          false" in result

    async def test_new_eks_workflow_skips_slack_when_instance_flag_set(
        self, script_generator, eks_file_location, workflow_context
    ):
        """DEPLOY_SKIP_SLACK_NOTIFICATION=true renders skip_slack: true so a
        UAT DevLift instance does not post to the shared build-alert channel."""
        existing_file = {"exists": False}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "infrastructure_type": "eks",
                "service_name": "my-svc",
                "environment": "staging",
                "language": "golang",
                "org_name": "core",
                "aws_region": "ap-south-1",
                "go_version": "1.23",
            },
        }

        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)), \
             patch(f"{MODULE}.settings") as mock_settings:
            mock_settings.deploy_skip_slack_notification = True
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=eks_file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        assert "skip_slack:          true" in result
        assert "{{SKIP_SLACK}}" not in result

    async def test_new_eks_workflow_includes_trigger_paths(
        self, script_generator, eks_file_location, workflow_context
    ):
        """Additional trigger paths from the UI land in the EKS workflow's
        on.push.paths block (non-golang languages)."""
        # Arrange
        existing_file = {"exists": False}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "infrastructure_type": "eks",
                "service_name": "my-svc",
                "environment": "staging",
                "language": "java",
                "additional_trigger_paths": ["shared", "libs/common"],
            },
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=eks_file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert
        assert "    paths:" in result
        assert "      - 'shared/**'" in result
        assert "      - 'libs/common/**'" in result
        assert "      - 'configs/**'" in result
        assert "{{FOLDER_PATH_FILTER}}" not in result

    async def test_new_eks_workflow_without_paths_has_no_paths_block(
        self, script_generator, eks_file_location, workflow_context
    ):
        """No trigger paths in the UI → no paths block, and no leftover
        placeholder or blank line."""
        # Arrange
        existing_file = {"exists": False}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "infrastructure_type": "eks",
                "service_name": "my-svc",
                "environment": "staging",
                "language": "java",
            },
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=eks_file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert
        assert "    paths:" not in result
        assert "{{FOLDER_PATH_FILTER}}" not in result
        assert "      - main\n  workflow_dispatch:" in result  # no blank line left

    async def test_new_eks_golang_workflow_gets_no_paths_filter(
        self, script_generator, eks_file_location, workflow_context
    ):
        """Current rule (pending a product decision): golang services trigger
        on any push — the paths filter is omitted even when the UI sends
        trigger paths."""
        # Arrange
        existing_file = {"exists": False}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "infrastructure_type": "eks",
                "service_name": "my-svc",
                "environment": "staging",
                "language": "golang",
                "additional_trigger_paths": ["shared"],
            },
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=eks_file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert
        assert "    paths:" not in result
        assert "{{FOLDER_PATH_FILTER}}" not in result

    async def test_ecs_redeploy_patches_scalars_and_preserves_hand_edits(
        self, script_generator, ecs_file_location, workflow_context
    ):
        """A redeploy changing go_version and cluster touches ONLY those two
        lines — the hand-added env var, cache step, paths filter, and custom
        build command are byte-identical."""
        # Arrange
        existing_file = {"exists": True, "content": ECS_EXISTING}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "infrastructure_type": "ecs",
                "service_name": "my-svc",
                "environment": "dev",
                "language_ref_code": "GO_1_23",
                "go_version": "1.23",
                "ecs_cluster": "new-cluster",
            },
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=ecs_file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert: exactly two lines changed
        expected = ECS_EXISTING.replace(
            "go-version: '1.22'", "go-version: '1.23'"
        ).replace(
            "ECS_CLUSTER: core-stage-cluster", "ECS_CLUSTER: new-cluster"
        )
        assert result == expected
        assert 'EXTRA_FLAG: "added-by-hand"' in result
        assert "# hand-added cache step — must survive redeploys" in result
        assert "- 'services/my-svc/**'   # hand-tuned monorepo filter" in result
        assert "go build -o app ./cmd/custom" in result

    async def test_ecs_redeploy_without_managed_fields_changes_nothing(
        self, script_generator, ecs_file_location, workflow_context
    ):
        """A redeploy whose config carries no changed managed fields leaves
        the file byte-identical — and stages nothing (skip_commit)."""
        # Arrange
        existing_file = {"exists": True, "content": ECS_EXISTING}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "infrastructure_type": "ecs",
                "service_name": "my-svc",
                "environment": "dev",
                "language_ref_code": "GO_1_23",
            },
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=ecs_file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert: identical content, no commit staged
        assert result == ECS_EXISTING
        assert workflow_context.staged_files == []
        assert workflow_context.commit_messages == {}

    async def test_ecs_service_name_is_not_rederived_on_update(
        self, script_generator, ecs_file_location, workflow_context
    ):
        """Without an explicit ecs_service in the config, the SERVICE_NAME env
        line keeps whatever the file has (no silent re-derivation)."""
        # Arrange: SERVICE_NAME hand-changed in the repo
        existing = ECS_EXISTING.replace(
            "SERVICE_NAME: my_svc-dev", "SERVICE_NAME: my-svc-custom-name"
        )
        existing_file = {"exists": True, "content": existing}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "infrastructure_type": "ecs",
                "service_name": "my-svc",
                "environment": "dev",
                "language_ref_code": "GO_1_23",
                "go_version": "1.23",
            },
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=ecs_file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert
        assert "SERVICE_NAME: my-svc-custom-name" in result

    async def test_eks_redeploy_patches_with_inputs_and_preserves_hand_edits(
        self, script_generator, eks_file_location, workflow_context
    ):
        """An EKS redeploy patches with:-inputs and the trigger branch; the
        hand-added with:-input and extra trigger branch survive."""
        # Arrange
        existing_file = {"exists": True, "content": EKS_EXISTING}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "infrastructure_type": "eks",
                "service_name": "my-svc",
                "environment": "staging",
                "aws_region": "eu-west-2",
                "go_version": "1.24",
                "branches": ["develop"],
            },
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=eks_file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert: exactly three lines changed
        expected = EKS_EXISTING.replace(
            "aws_region:          ap-south-1", "aws_region:          eu-west-2"
        ).replace(
            'go_version:          "1.22"', 'go_version:          "1.24"'
        ).replace(
            "      - main\n      - hotfix/manual",
            "      - develop\n      - hotfix/manual",
        )
        assert result == expected
        assert "timeout_minutes:     30   # added by hand" in result
        assert "- hotfix/manual    # added by hand" in result

    async def test_eks_java_redeploy_patches_build_path(
        self, script_generator, eks_file_location, workflow_context
    ):
        """Non-golang EKS files carry the input as `build_path` — a build path
        change must patch that line on an existing file."""
        # Arrange: java-style rendered file
        existing = (
            "on:\n"
            "  push:\n"
            "    branches:\n"
            "      - main\n"
            "  workflow_dispatch:\n"
            "\n"
            "jobs:\n"
            "  build:\n"
            "    uses: Vance-Club/shared-lib/.github/workflows/build.yaml@main\n"
            "    with:\n"
            "      service_name:    my-svc-service\n"
            "      environment:     stage\n"
            '      java_version:    "17"\n'
            "      build_path:      services/payment\n"
            "    secrets: inherit\n"
        )
        existing_file = {"exists": True, "content": existing}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "infrastructure_type": "eks",
                "service_name": "my-svc",
                "environment": "staging",
                "language": "java",
                "build_path": "apps/payment",
            },
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=eks_file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert: only the build_path line changed
        assert result == existing.replace(
            "build_path:      services/payment", "build_path:      apps/payment"
        )

    async def test_eks_golang_build_path_is_normalized(
        self, script_generator, eks_file_location, workflow_context
    ):
        """A bare 'cmd' from the UI becomes './cmd' — `go build cmd` would
        resolve an import path and fail; already-anchored values pass through."""
        # Arrange
        existing_file = {"exists": True, "content": EKS_EXISTING}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "infrastructure_type": "eks",
                "service_name": "my-svc",
                "environment": "staging",
                "language": "golang",
                "build_path": "cmd",
            },
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=eks_file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert
        assert "golang_build_path:   ./cmd" in result
        assert "golang_build_path:   cmd" not in result.replace("./cmd", "")

    async def test_ecs_redeploy_appends_new_trigger_paths_keeps_existing(
        self, script_generator, ecs_file_location, workflow_context
    ):
        """Append-only trigger paths: a new UI path is added to the existing
        paths block; the hand-tuned entry (and its comment) stays."""
        # Arrange
        existing_file = {"exists": True, "content": ECS_EXISTING}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "infrastructure_type": "ecs",
                "service_name": "my-svc",
                "environment": "dev",
                "language_ref_code": "GO_1_23",
                "other_paths": ["shared"],
            },
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=ecs_file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert: the extra is appended, the hand entry untouched — and the
        # generator's own workflow-file entry is NOT introduced into a block
        # devlift did not create.
        assert "      - 'shared/**'" in result
        assert ".github/workflows/deploy-my-svc-dev.yml" not in result
        assert "- 'services/my-svc/**'   # hand-tuned monorepo filter" in result
        # appended inside the paths block, before workflow_dispatch
        assert result.index("'shared/**'") < result.index("workflow_dispatch")

    async def test_ecs_redeploy_trigger_paths_already_present_is_noop(
        self, script_generator, ecs_file_location, workflow_context
    ):
        """Entries the file already carries are not duplicated — a redeploy
        with matching paths leaves the file byte-identical."""
        # Arrange: file already has the UI's path AND the workflow-file entry
        existing = ECS_EXISTING.replace(
            "      - 'services/my-svc/**'   # hand-tuned monorepo filter",
            "      - 'services/my-svc/**'   # hand-tuned monorepo filter\n"
            "      - '.github/workflows/deploy-my-svc-dev.yml'",
        )
        existing_file = {"exists": True, "content": existing}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "infrastructure_type": "ecs",
                "service_name": "my-svc",
                "environment": "dev",
                "language_ref_code": "GO_1_23",
                "other_paths": ["services/my-svc"],
            },
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=ecs_file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert
        assert result == existing


    async def test_eks_redeploy_inserts_paths_block_when_absent(
        self, script_generator, eks_file_location, workflow_context
    ):
        """An EKS file without a paths block gets one created after branches
        when the UI sends trigger paths; hand-added branches stay."""
        # Arrange: a java service's file, matching the config's language
        # (a mismatch would correctly re-render instead of patching)
        existing_java = EKS_EXISTING.replace(
            "language:            golang", "language:            java"
        )
        existing_file = {"exists": True, "content": existing_java}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "infrastructure_type": "eks",
                "service_name": "my-svc",
                "environment": "staging",
                "language": "java",
                "additional_trigger_paths": ["shared"],
            },
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=eks_file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert
        assert "    paths:\n      - 'shared/**'\n      - 'configs/**'\n" in result
        assert "- hotfix/manual    # added by hand" in result
        assert result.index("- hotfix/manual") < result.index("paths:")

    async def test_eks_redeploy_finds_paths_block_behind_a_comment(
        self, script_generator, eks_file_location, workflow_context
    ):
        """A paths block separated from the branches list by a comment line is
        still THE block: entries merge into it, never into a second `paths:`
        key. KairosV2's frontend workflow is the shape."""
        # Arrange
        existing = EKS_EXISTING.replace(
            "language:            golang", "language:            java"
        ).replace(
            "      - hotfix/manual    # added by hand\n",
            "      - hotfix/manual    # added by hand\n"
            "    # Only when the frontend image's inputs change.\n"
            "    paths:\n"
            '      - "frontend/**"\n'
            '      - ".dockerignore"\n',
        )
        existing_file = {"exists": True, "content": existing}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "infrastructure_type": "eks",
                "service_name": "my-svc",
                "environment": "staging",
                "language": "java",
                "dockerfile_path": "frontend/Dockerfile",
                "other_paths": ["frontend", ".dockerignore"],
            },
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=eks_file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert: one paths key, the comment kept, nothing repeated, and the
        # hand-written filter left exactly as it was — the generator's own
        # lines are not introduced into a block devlift did not create.
        assert result.count("    paths:") == 1
        assert (
            "    # Only when the frontend image's inputs change.\n"
            "    paths:\n"
            '      - "frontend/**"\n'
            '      - ".dockerignore"\n'
            "  workflow_dispatch:"
        ) in result
        assert "'.dockerignore/**'" not in result
        assert "'frontend/Dockerfile'" not in result
        assert "'configs/**'" not in result

    async def test_eks_redeploy_replaces_tracked_build_path_entries(
        self, script_generator, eks_file_location, workflow_context
    ):
        """A block that carries the generator's own lines keeps tracking the
        record: a changed build path swaps the old folder and configs entries
        for the new ones, the Dockerfile entry stays, the new extra joins."""
        # Arrange: a java file devlift generated earlier, filter included
        existing = EKS_EXISTING.replace(
            "language:            golang", "language:            java"
        ).replace(
            "      golang_build_path:   ./cmd\n",
            "      build_path:      services/payment\n",
        ).replace(
            "      - hotfix/manual    # added by hand\n",
            "      - hotfix/manual    # added by hand\n"
            "    paths:\n"
            "      - 'services/payment/**'\n"
            "      - 'Dockerfile'\n"
            "      - 'configs/services/payment/**'\n",
        )
        existing_file = {"exists": True, "content": existing}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "infrastructure_type": "eks",
                "service_name": "my-svc",
                "environment": "staging",
                "language": "java",
                "build_path": "apps/payment",
                "dockerfile_path": "Dockerfile",
                "other_paths": ["shared"],
            },
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=eks_file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert
        assert result.count("    paths:") == 1
        assert "'services/payment/**'" not in result
        assert "'configs/services/payment/**'" not in result
        assert "      - 'apps/payment/**'" in result
        assert "      - 'configs/apps/payment/**'" in result
        assert "      - 'Dockerfile'" in result
        assert "      - 'shared/**'" in result
        assert result.index("'shared/**'") < result.index("workflow_dispatch")

    def test_folder_path_filter_writes_files_and_globs_verbatim(
        self, script_generator
    ):
        """Only a folder gets /**. A file or a glob among other_paths is
        written as it is — `.dockerignore/**` matches nothing."""
        block = script_generator._generate_folder_path_filter(
            {
                "build_path": "app",
                "dockerfile_path": "Dockerfile",
                "other_paths": ["shared", ".dockerignore", "*.py", "docs/README"],
            },
            "java",
        )
        lines = [ln.strip() for ln in block.splitlines() if ln.strip().startswith("-")]
        assert lines == [
            "- 'app/**'",
            "- 'Dockerfile'",
            "- 'shared/**'",
            "- '.dockerignore'",
            "- '*.py'",
            "- 'docs/README'",
            "- 'configs/app/**'",
        ]

    async def test_eks_language_change_rebuilds_from_new_template(
        self, script_generator, eks_file_location, workflow_context
    ):
        """Switching language is a rebuild event: the go-rendered file is
        replaced by a fresh render of the new language's template."""
        # Arrange: existing file is a go render; config now says nodejs
        existing_file = {"exists": True, "content": EKS_EXISTING}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "infrastructure_type": "eks",
                "service_name": "my-svc",
                "environment": "staging",
                "language": "nodejs",
                "node_version": "20",
            },
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=eks_file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert: fresh nodejs render, go-specific inputs gone
        assert "language:        nodejs" in result
        assert 'node_version:    "20"' in result
        assert "golang_binary_name" not in result
        assert "go_version" not in result
        # hand edit from the abandoned go pipeline is dropped deliberately
        assert "timeout_minutes" not in result

    async def test_ecs_language_change_rebuilds_from_new_template(
        self, script_generator, ecs_file_location, workflow_context
    ):
        """Same rebuild rule for ECS: a go pipeline file redeployed as nodejs
        is re-rendered from the nodejs template."""
        # Arrange
        existing_file = {"exists": True, "content": ECS_EXISTING}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "infrastructure_type": "ecs",
                "service_name": "my-svc",
                "environment": "dev",
                "language_ref_code": "NODEJS_20",
                "node_version": "20",
            },
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=ecs_file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert: nodejs pipeline, go steps gone
        assert "node-version: '20'" in result
        assert "setup-go" not in result
        assert "go build" not in result
        # hand-added step from the go pipeline is dropped deliberately
        assert "Cache modules" not in result

    async def test_eks_redeploy_patches_version_from_language_version(
        self, script_generator, eks_file_location, workflow_context
    ):
        """Real snapshots carry only language_version (not go_version etc.) —
        a version bump must still patch the file's version line."""
        # Arrange
        existing_file = {"exists": True, "content": EKS_EXISTING}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "infrastructure_type": "eks",
                "service_name": "my-svc",
                "environment": "staging",
                "language": "golang",
                "language_version": "1.25",
            },
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=eks_file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert: only the version line changed
        expected = EKS_EXISTING.replace(
            'go_version:          "1.22"', 'go_version:          "1.25"'
        )
        assert result == expected

    async def test_ecs_redeploy_patches_version_from_language_version(
        self, script_generator, ecs_file_location, workflow_context
    ):
        """Same for ECS: language_version alone must bump go-version."""
        # Arrange
        existing_file = {"exists": True, "content": ECS_EXISTING}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "infrastructure_type": "ecs",
                "service_name": "my-svc",
                "environment": "dev",
                "language_ref_code": "GO_1_25",
                "language_version": "1.25",
            },
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=ecs_file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert: only the version line changed
        expected = ECS_EXISTING.replace("go-version: '1.22'", "go-version: '1.25'")
        assert result == expected

    async def test_second_generate_patches_staged_content_not_repo(
        self, script_generator, ecs_file_location, workflow_context
    ):
        """Two deploys in one run: the second builds on the first's staged
        content (no second git fetch), so both patches end up in the file."""
        # Arrange
        existing_file = {"exists": True, "content": ECS_EXISTING}
        first_queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "infrastructure_type": "ecs",
                "service_name": "my-svc",
                "environment": "dev",
                "language_ref_code": "GO_1_23",
                "go_version": "1.23",
            },
        }
        second_queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "infrastructure_type": "ecs",
                "service_name": "my-svc",
                "environment": "dev",
                "language_ref_code": "GO_1_23",
                "ecs_cluster": "new-cluster",
            },
        }

        # Act: first deploy fetches from git
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)) as mock_get:
            first = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=ecs_file_location,
                queue_dict=first_queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )
        assert mock_get.await_count == 1
        assert "go-version: '1.23'" in first

        # Act: second deploy in the same workflow run
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)) as mock_get:
            second = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=ecs_file_location,
                queue_dict=second_queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert: staged cache was used, both patches present
        assert mock_get.await_count == 0
        assert "go-version: '1.23'" in second
        assert "ECS_CLUSTER: new-cluster" in second

    async def test_new_ecs_python_workflow_carries_language_marker(
        self, script_generator, ecs_file_location, workflow_context
    ):
        """The python template has no language-specific steps, so fresh
        renders carry an explicit marker comment — and detection round-trips."""
        # Arrange
        existing_file = {"exists": False}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "infrastructure_type": "ecs",
                "service_name": "my-svc",
                "environment": "dev",
                "language_ref_code": "PYTHON_3_12",
            },
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=ecs_file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert
        assert "# devlift-language: python" in result
        assert script_generator._detect_workflow_language(result, "ecs") == "python"

    async def test_ecs_switch_away_from_old_python_file_rebuilds(
        self, script_generator, ecs_file_location, workflow_context
    ):
        """A python file rendered BEFORE the marker existed (docker-only, no
        setup steps) is recognized by elimination — switching it to java
        re-renders instead of patching java values into a python pipeline."""
        # Arrange: old-style python render — docker build + force deploy only
        old_python = (
            "name: Deploy my-svc to dev\n"
            "on:\n"
            "  push:\n"
            "    branches:\n"
            "      - main\n"
            "env:\n"
            "  AWS_REGION: ap-south-1\n"
            "  ECR_REPOSITORY: 878.dkr.ecr/my-svc\n"
            "jobs:\n"
            "  build-and-deploy:\n"
            "    steps:\n"
            "      - name: Checkout code\n"
            "        uses: actions/checkout@v4\n"
            "      - name: Build, tag, and push image to Amazon ECR\n"
            "        run: docker build -t x .\n"
            "      - name: Force ECS Deployment\n"
            "        run: |\n"
            "          aws ecs update-service \\\n"
            "            --cluster old-cluster \\\n"
            "            --force-new-deployment\n"
        )
        existing_file = {"exists": True, "content": old_python}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "infrastructure_type": "ecs",
                "service_name": "my-svc",
                "environment": "dev",
                "language_ref_code": "JAVA_21",
                "language_version": "21",
            },
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=ecs_file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert: full java rebuild, not a patched python pipeline
        assert "gradle" in result.lower()
        assert "java-version: 21" in result
        assert "old-cluster" not in result

    async def test_ecs_stage_environment_normalizes_to_stg(
        self, script_generator, ecs_file_location, workflow_context
    ):
        """'stage' (a live env value) must map to stg like staging/qa —
        on create AND when patching an existing file."""
        # Arrange / Act: create
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value={"exists": False})):
            created = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=ecs_file_location,
                queue_dict={
                    "id": 1,
                    "code": "queue-001",
                    "config_snapshot": {
                        "infrastructure_type": "ecs",
                        "service_name": "my-svc",
                        "environment": "stage",
                        "language_ref_code": "GO_1_23",
                    },
                },
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Act: patch an existing file
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value={"exists": True, "content": ECS_EXISTING})):
            patched = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=ecs_file_location,
                queue_dict={
                    "id": 1,
                    "code": "queue-001",
                    "config_snapshot": {
                        "infrastructure_type": "ecs",
                        "service_name": "my-svc",
                        "environment": "stage",
                        "language_ref_code": "GO_1_23",
                    },
                },
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert
        assert "ENVIRONMENT: stg" in created
        assert "ENVIRONMENT: stg" in patched

    async def test_ecs_production_environment_gets_prod_gate(
        self, script_generator, ecs_file_location, workflow_context
    ):
        """'production' must get the GitHub prod approval gate and the prod
        env code, same as 'prod'."""
        # Arrange
        existing_file = {"exists": False}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "infrastructure_type": "ecs",
                "service_name": "my-svc",
                "environment": "production",
                "language_ref_code": "GO_1_23",
            },
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=ecs_file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert
        assert "    environment: prod" in result  # approval gate present
        assert "ENVIRONMENT: prod" in result

    async def test_ecs_gradle_to_maven_switch_rebuilds(
        self, script_generator, ecs_file_location, workflow_context
    ):
        """Maven and Gradle are different pipelines (mvnw vs gradlew, JAR
        dirs) — switching build tool must re-render, not patch."""
        # Arrange: a gradle-rendered java file
        gradle_existing = (
            "on:\n"
            "  push:\n"
            "    branches:\n"
            "      - main\n"
            "env:\n"
            "  ENVIRONMENT: dev\n"
            "jobs:\n"
            "  build-and-deploy:\n"
            "    steps:\n"
            "      - name: Set up JDK 17\n"
            "        uses: actions/setup-java@v4\n"
            "        with:\n"
            "          java-version: 17\n"
            "      - name: Build\n"
            "        run: ./gradlew clean build -x test\n"
        )
        existing_file = {"exists": True, "content": gradle_existing}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "infrastructure_type": "ecs",
                "service_name": "my-svc",
                "environment": "dev",
                "language_ref_code": "JAVA-MAVEN_17",
                "language_version": "17",
            },
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=ecs_file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert: full maven rebuild
        assert "mvnw" in result
        assert "gradlew" not in result

    async def test_ecs_bare_java_file_is_compatible_with_java_config(
        self, script_generator, ecs_file_location, workflow_context
    ):
        """A java file whose build tool can't be told (no gradle/maven
        markers) must NOT trigger a rebuild — it patches in place."""
        # Arrange: java markers only, no build-tool fingerprint
        bare_java = (
            "on:\n"
            "  push:\n"
            "    branches:\n"
            "      - main\n"
            "env:\n"
            "  ENVIRONMENT: dev\n"
            "jobs:\n"
            "  build-and-deploy:\n"
            "    steps:\n"
            "      - name: Set up JDK\n"
            "        uses: actions/setup-java@v4\n"
            "        with:\n"
            "          java-version: 17\n"
            "      - name: Build\n"
            "        run: ./custom-build.sh\n"
        )
        existing_file = {"exists": True, "content": bare_java}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "infrastructure_type": "ecs",
                "service_name": "my-svc",
                "environment": "dev",
                "language_ref_code": "JAVA_21",
                "language_version": "21",
            },
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=ecs_file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert: patched in place (version bumped, custom build kept)
        assert result == bare_java.replace("java-version: 17", "java-version: 21")
        assert "./custom-build.sh" in result

    async def test_new_ecs_python_workflow_uses_env_vars_for_deploy(
        self, script_generator, ecs_file_location, workflow_context
    ):
        """The python template now routes cluster/service through env vars
        (like every other language), so redeploys can patch them."""
        # Arrange
        existing_file = {"exists": False}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "infrastructure_type": "ecs",
                "service_name": "my-svc",
                "environment": "dev",
                "language_ref_code": "PYTHON_3_12",
                "ecs_cluster": "core-stage-cluster",
                "ecs_service": "my-svc-dev",
            },
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=ecs_file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert
        assert "ECS_CLUSTER: core-stage-cluster" in result
        assert "SERVICE_NAME: my-svc-dev" in result
        assert "--cluster ${{ env.ECS_CLUSTER }}" in result
        assert "--service ${{ env.SERVICE_NAME }}" in result

    async def test_ecs_old_python_file_literal_cluster_is_patched(
        self, script_generator, ecs_file_location, workflow_context
    ):
        """Old python renders baked cluster/service literals into the deploy
        command — a cluster change must patch those flags in place, while
        env-ref flag lines (other languages) are never touched."""
        # Arrange: old-style python file with literal flags
        old_python = (
            "on:\n"
            "  push:\n"
            "    branches:\n"
            "      - main\n"
            "env:\n"
            "  AWS_REGION: ap-south-1\n"
            "jobs:\n"
            "  build-and-deploy:\n"
            "    steps:\n"
            "      - name: Build, tag, and push image to Amazon ECR\n"
            "        run: docker build -t x .\n"
            "      - name: Force ECS Deployment\n"
            "        run: |\n"
            "          aws ecs update-service \\\n"
            "            --cluster old-cluster \\\n"
            "            --service old-svc \\\n"
            "            --force-new-deployment \\\n"
            "            --region ${{ env.AWS_REGION }}\n"
        )
        existing_file = {"exists": True, "content": old_python}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "infrastructure_type": "ecs",
                "service_name": "my-svc",
                "environment": "dev",
                "language_ref_code": "PYTHON_3_12",
                "ecs_cluster": "new-cluster",
                "ecs_service": "new-svc",
            },
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=ecs_file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert: only the two literal flags changed
        expected = old_python.replace(
            "--cluster old-cluster", "--cluster new-cluster"
        ).replace("--service old-svc", "--service new-svc")
        assert result == expected

    async def test_result_is_staged_for_commit_with_message(
        self, script_generator, ecs_file_location, workflow_context
    ):
        """The generated content is staged for the git commit, stored in the
        workflow responses, and a commit message line is recorded."""
        # Arrange
        existing_file = {"exists": False}
        queue_dict = {
            "id": 1,
            "code": "queue-001",
            "config_snapshot": {
                "infrastructure_type": "ecs",
                "service_name": "my-svc",
                "environment": "dev",
                "language_ref_code": "GO_1_23",
            },
        }

        # Act
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value=existing_file)):
            result = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=ecs_file_location,
                queue_dict=queue_dict,
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert
        stored = workflow_context.script_gen_responses[1]["ecs_pipeline"]["main"]
        assert stored["original_content"] == result
        assert len(workflow_context.staged_files) == 1
        assert workflow_context.staged_files[0]["content"] == result
        assert "queue-001" in workflow_context.commit_messages["owner/app-repo|||main"]
