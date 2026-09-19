"""
Unit tests for AsporaK8sManifestsScriptGenComponent (patch-in-place).

The rule under test: an existing values.yaml is the base and only the managed
nested fields (resources, keda replicas, groupOrder, capacityType / port,
health, ingress path) may change — hand-added keys, comments, and the
pipeline-owned image.tag must survive a redeploy. Chart.yaml/templates keep
their skip-if-exists behavior.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.plugin.aspora.script_gen_components.aspora_k8s_manifests_script_gen_component import (
    AsporaK8sManifestsScriptGenComponent,
)

GET_CONTENT_TARGET = (
    "app.plugin.aspora.script_gen_components."
    "aspora_k8s_manifests_script_gen_component.GitOpsHandler.get_content"
)

# A rendered-style env values.yaml with hand edits sprinkled in.
ENV_VALUES_EXISTING = """# Stage ap-south-1 deployment overrides for my-svc-service.
image:
  tag: v1.4.2
resources:
  requests:
    cpu: 500m
    memory: 512Mi
  limits:
    cpu: 500m
    memory: 512Mi

ingress:
  groupOrder: "10"

# HA: keep at least 2 replicas so PodDisruptionBudget renders (skipped at 1)
keda:
  minReplicas: 2
  maxReplicas: 4

scheduling:
  capacityType: on-demand

# hand-added: JVM tuning flags
env:
  - name: JAVA_OPTS
    value: "-Xmx400m"

nodeSelector:
  workload: general   # pinned by platform team
"""

# A rendered-style chart values.yaml with a hand-added block.
CHART_VALUES_EXISTING = """appName: my-svc-service
kind: api
podType: core-platform
containerPort: 8080
healthCheckPath: /my-svc-service/actuator/health
# ALB listener path
ingress:
  path: /my-svc-service

podSecurityContext:       { runAsNonRoot: false }
containerSecurityContext: { runAsNonRoot: false }

# hand-added override — must survive redeploys
serviceMonitor:
  enabled: true
"""


class TestAsporaK8sManifestsScriptGenComponent:
    @pytest.fixture
    def script_generator(self):
        return AsporaK8sManifestsScriptGenComponent()

    @pytest.fixture
    def workflow_context(self):
        return SimpleNamespace(
            skip_commit=False,
            staged_files=[],
            commit_messages={},
            script_gen_responses={1: {}},
        )

    @staticmethod
    def _file_location(script_gen_key, file_path):
        return SimpleNamespace(
            repo="owner/k8s-manifests",
            file_path=file_path,
            base_branch="main",
            feature_branch="feature/test",
            target_branch="main",
            script_gen_key=script_gen_key,
            queue_code="queue-001",
            config={"service_type": "API"},
        )

    @staticmethod
    def _queue_dict(config_snapshot):
        return {"id": 1, "code": "queue-001", "config_snapshot": config_snapshot}

    async def test_chart_meta_skip_if_exists_keeps_file_verbatim(
        self, script_generator, workflow_context
    ):
        """Chart.yaml already in the repo is staged exactly as-is."""
        # Arrange
        existing = "apiVersion: v2\nname: my-svc-service\nversion: 9.9.9   # hand-pinned\n"
        existing_file = {"exists": True, "content": existing}
        file_location = self._file_location(
            "k8s_chart_meta", "charts/services/my-svc-service/Chart.yaml"
        )
        queue_dict = self._queue_dict({"service_name": "my-svc", "environment": "stage"})

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

    async def test_new_env_values_renders_template(
        self, script_generator, workflow_context
    ):
        """A new service's env values.yaml renders from the template."""
        # Arrange
        existing_file = {"exists": False}
        file_location = self._file_location(
            "k8s_env_values", "environments/core/stage/ap-south-1/my-svc-service/values.yaml"
        )
        queue_dict = self._queue_dict({
            "service_name": "my-svc",
            "environment": "staging",
            "image_tag": "abc123",
            "ingress_group_order": "20",
            "compute": "spot",
        })

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
        assert "tag: abc123" in result
        assert 'groupOrder: "20"' in result
        assert "capacityType: spot" in result
        assert "minReplicas: 2" in result  # hpa defaults
        assert "{{" not in result  # no unfilled placeholders

    async def test_env_values_redeploy_patches_fields_and_preserves_hand_edits(
        self, script_generator, workflow_context
    ):
        """Changing resources/replicas/order/capacity touches ONLY those
        lines — requests vs limits stay independent, and the hand-added env
        block, nodeSelector, and comments are byte-identical."""
        # Arrange
        existing_file = {"exists": True, "content": ENV_VALUES_EXISTING}
        file_location = self._file_location(
            "k8s_env_values", "environments/core/stage/ap-south-1/my-svc-service/values.yaml"
        )
        queue_dict = self._queue_dict({
            "service_name": "my-svc",
            "environment": "stage",
            "cpu_requested": "1",          # -> 1000m, requests only
            "memory_limit": "1Gi",         # -> limits only
            "ingress_group_order": "25",
            "compute": "spot",
            "hpa": {"min_replicas": 3, "max_replicas": 6},
        })

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

        # Assert: exactly the managed lines changed
        expected = (
            ENV_VALUES_EXISTING
            .replace("    cpu: 500m\n    memory: 512Mi\n  limits:",
                     "    cpu: 1000m\n    memory: 512Mi\n  limits:")
            .replace("  limits:\n    cpu: 500m\n    memory: 512Mi",
                     "  limits:\n    cpu: 500m\n    memory: 1Gi")
            .replace('groupOrder: "10"', 'groupOrder: "25"')
            .replace("minReplicas: 2", "minReplicas: 3")
            .replace("maxReplicas: 4", "maxReplicas: 6")
            .replace("capacityType: on-demand", "capacityType: spot")
        )
        assert result == expected
        assert "tag: v1.4.2" in result  # pipeline-owned, untouched
        assert 'value: "-Xmx400m"' in result
        assert "workload: general   # pinned by platform team" in result

    async def test_env_values_redeploy_never_touches_image_tag(
        self, script_generator, workflow_context
    ):
        """Even when the snapshot carries an image_tag, an existing file's
        tag stays — the deploy/rollback pipeline owns it."""
        # Arrange
        existing_file = {"exists": True, "content": ENV_VALUES_EXISTING}
        file_location = self._file_location(
            "k8s_env_values", "environments/core/stage/ap-south-1/my-svc-service/values.yaml"
        )
        queue_dict = self._queue_dict({
            "service_name": "my-svc",
            "environment": "stage",
            "image_tag": "brand-new-tag",
        })

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
        assert "tag: v1.4.2" in result
        assert "brand-new-tag" not in result

    async def test_env_values_redeploy_without_managed_fields_is_byte_identical(
        self, script_generator, workflow_context
    ):
        """A config with no managed fields changes nothing at all."""
        # Arrange
        existing_file = {"exists": True, "content": ENV_VALUES_EXISTING}
        file_location = self._file_location(
            "k8s_env_values", "environments/core/stage/ap-south-1/my-svc-service/values.yaml"
        )
        queue_dict = self._queue_dict({"service_name": "my-svc", "environment": "stage"})

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
        assert result == ENV_VALUES_EXISTING

    async def test_env_values_qa_file_without_keda_replicas_is_not_broken(
        self, script_generator, workflow_context
    ):
        """A qa-style file (keda disabled, no replica keys) accepts an hpa
        config without inserting anything — absent paths are skipped."""
        # Arrange
        qa_existing = (
            "image:\n"
            "  tag: v2.0.0\n"
            "# QA cluster runs without KEDA — keep the ScaledObject off.\n"
            "keda:\n"
            "  enabled: false\n"
            "scheduling:\n"
            "  capacityType: on-demand\n"
        )
        existing_file = {"exists": True, "content": qa_existing}
        file_location = self._file_location(
            "k8s_env_values", "environments/core/qa/ap-south-1/my-svc-service/values.yaml"
        )
        queue_dict = self._queue_dict({
            "service_name": "my-svc",
            "environment": "qa",
            "hpa": {"min_replicas": 3, "max_replicas": 6},
            "compute": "spot",
        })

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

        # Assert: only capacityType changed; no replica lines appeared
        assert result == qa_existing.replace(
            "capacityType: on-demand", "capacityType: spot"
        )
        assert "minReplicas" not in result


    async def test_new_env_values_fixed_replica_count_maps_to_keda_min_max(
        self, script_generator, workflow_context
    ):
        """Autoscaling off + replica_count → minReplicas == maxReplicas ==
        count (the chart's only replica mechanism is KEDA)."""
        # Arrange
        existing_file = {"exists": False}
        file_location = self._file_location(
            "k8s_env_values", "environments/core/stage/ap-south-1/my-svc-service/values.yaml"
        )
        queue_dict = self._queue_dict({
            "service_name": "my-svc",
            "environment": "stage",
            "replica_count": "3",
            "hpa": {"enabled": False},
        })

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
        assert "minReplicas: 3" in result
        assert "maxReplicas: 3" in result
        assert "triggers:" not in result  # no thresholds sent



    async def test_env_values_redeploy_fixed_replica_count_patches_min_max(
        self, script_generator, workflow_context
    ):
        """Autoscaling off + replica_count on an existing file patches both
        keda replica lines to the count."""
        # Arrange
        existing_file = {"exists": True, "content": ENV_VALUES_EXISTING}
        file_location = self._file_location(
            "k8s_env_values", "environments/core/stage/ap-south-1/my-svc-service/values.yaml"
        )
        queue_dict = self._queue_dict({
            "service_name": "my-svc",
            "environment": "stage",
            "replica_count": "5",
            "hpa": {"enabled": False},
        })

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
        expected = ENV_VALUES_EXISTING.replace(
            "minReplicas: 2", "minReplicas: 5"
        ).replace("maxReplicas: 4", "maxReplicas: 5")
        assert result == expected

    async def test_env_values_qa_file_ignores_thresholds(
        self, script_generator, workflow_context
    ):
        """A qa-style file (keda disabled, no maxReplicas line) is not touched
        by thresholds — nothing to anchor the triggers block to."""
        # Arrange
        qa_existing = (
            "image:\n"
            "  tag: v2.0.0\n"
            "keda:\n"
            "  enabled: false\n"
        )
        existing_file = {"exists": True, "content": qa_existing}
        file_location = self._file_location(
            "k8s_env_values", "environments/core/qa/ap-south-1/my-svc-service/values.yaml"
        )
        queue_dict = self._queue_dict({
            "service_name": "my-svc",
            "environment": "qa",
            "hpa": {"enabled": True, "cpu_threshold": 60},
        })

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
        assert result == qa_existing

    async def test_hpa_thresholds_are_never_written(
        self, script_generator, workflow_context
    ):
        """Real service files carry only minReplicas/maxReplicas — utilization
        overrides are deliberately never written (the chart defaults apply),
        on create AND on update, even when the UI sends threshold values."""
        # Arrange / Act: create with thresholds in the payload
        file_location = self._file_location(
            "k8s_env_values", "environments/core/stage/ap-south-1/my-svc-service/values.yaml"
        )
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value={"exists": False})):
            created = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=file_location,
                queue_dict=self._queue_dict({
                    "service_name": "my-svc",
                    "environment": "stage",
                    "hpa": {"enabled": True, "min_replicas": 2, "max_replicas": 5,
                            "cpu_threshold": 70, "memory_threshold": 80},
                }),
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Act: update an existing file that already has a hand-set override.
        # Fresh context — the shared one would serve the staged create render
        # from part 1 instead of the repo file.
        workflow_context = SimpleNamespace(
            skip_commit=False,
            staged_files=[],
            commit_messages={},
            script_gen_responses={1: {}},
        )
        existing = ENV_VALUES_EXISTING.replace(
            "  minReplicas: 2\n  maxReplicas: 4",
            "  minReplicas: 2\n  maxReplicas: 4\n  cpu:\n    utilization: 65",
        )
        with patch(GET_CONTENT_TARGET, new=AsyncMock(return_value={"exists": True, "content": existing})):
            patched = await script_generator.generate(
                tenant="aspora",
                repository=None,
                file_location=file_location,
                queue_dict=self._queue_dict({
                    "service_name": "my-svc",
                    "environment": "stage",
                    "hpa": {"enabled": True, "cpu_threshold": 70},
                }),
                workflow_context=workflow_context,
                upload_to_s3=False,
                db=None,
            )

        # Assert: create writes min/max only; update leaves everything alone
        assert "minReplicas: 2" in created and "maxReplicas: 5" in created
        assert "utilization" not in created
        assert patched == existing  # hand-set override untouched, nothing added

    async def test_env_values_compute_inserts_scheduling_block_when_absent(
        self, script_generator, workflow_context
    ):
        """Most pre-devlift env values files have no scheduling: block — a
        compute change must create it instead of silently doing nothing."""
        # Arrange: real-world-style file without a scheduling block
        existing = (
            "image:\n"
            "  tag: v3.1.0\n"
            "resources:\n"
            "  requests:\n"
            "    cpu: 500m\n"
            "    memory: 512Mi\n"
            "\n"
            "# hand-added override\n"
            "extraEnv:\n"
            "  - name: FEATURE_FLAG\n"
            "    value: \"on\"\n"
        )
        existing_file = {"exists": True, "content": existing}
        file_location = self._file_location(
            "k8s_env_values", "environments/core/stage/ap-south-1/my-svc-service/values.yaml"
        )
        queue_dict = self._queue_dict({
            "service_name": "my-svc",
            "environment": "stage",
            "compute": "spot",
        })

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

        # Assert: block appended at the end, everything else byte-identical
        assert result == existing + "\nscheduling:\n  capacityType: spot\n"
        assert "tag: v3.1.0" in result
        assert 'value: "on"' in result

    async def test_chart_values_redeploy_patches_fields_and_preserves_hand_edits(
        self, script_generator, workflow_context
    ):
        """Port/health/path changes patch their lines; appName and the
        hand-added serviceMonitor block stay byte-identical."""
        # Arrange
        existing_file = {"exists": True, "content": CHART_VALUES_EXISTING}
        file_location = self._file_location(
            "k8s_chart_values", "charts/services/my-svc-service/values.yaml"
        )
        queue_dict = self._queue_dict({
            "service_name": "my-svc",
            "environment": "stage",
            "port": "9090",
            "health": "/healthz",
            "service_path": "/api/my-svc/*",
        })

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

        # Assert: exactly three lines changed
        expected = (
            CHART_VALUES_EXISTING
            .replace("containerPort: 8080", "containerPort: 9090")
            .replace("healthCheckPath: /my-svc-service/actuator/health",
                     "healthCheckPath: /healthz")
            .replace("  path: /my-svc-service", "  path: /api/my-svc")
        )
        assert result == expected
        assert "appName: my-svc-service" in result
        assert "# hand-added override — must survive redeploys" in result
        assert "serviceMonitor:" in result

    async def test_new_chart_values_renders_template(
        self, script_generator, workflow_context
    ):
        """A new service's chart values.yaml renders from the template."""
        # Arrange
        existing_file = {"exists": False}
        file_location = self._file_location(
            "k8s_chart_values", "charts/services/my-svc-service/values.yaml"
        )
        queue_dict = self._queue_dict({
            "service_name": "my-svc",
            "environment": "stage",
            "port": "9090",
        })

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
        assert "appName: my-svc-service" in result
        assert "containerPort: 9090" in result
        assert "{{" not in result

    async def test_result_is_staged_for_commit_with_message(
        self, script_generator, workflow_context
    ):
        """The patched content is staged for the git commit, stored in the
        workflow responses, and a commit message line is recorded."""
        # Arrange
        existing_file = {"exists": True, "content": ENV_VALUES_EXISTING}
        file_location = self._file_location(
            "k8s_env_values", "environments/core/stage/ap-south-1/my-svc-service/values.yaml"
        )
        queue_dict = self._queue_dict({
            "service_name": "my-svc",
            "environment": "stage",
            "compute": "spot",
        })

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
        assert workflow_context.script_gen_responses[1]["k8s_env_values"]["original_content"] == result
        assert len(workflow_context.staged_files) == 1
        assert workflow_context.staged_files[0]["content"] == result
        assert "queue-001" in workflow_context.commit_messages["owner/k8s-manifests|||main"]
