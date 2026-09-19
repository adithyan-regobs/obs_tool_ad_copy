"""What an infrastructure UPDATE is not allowed to change.

Every case here was reachable with one authenticated request before the guard
existed, so each test names the real consequence rather than just the rule.
"""

import pytest
from fastapi import HTTPException

from app.core.enum import EnvironmentEnum, ResourceStatusEnum
from app.domain.validators import infrastructure_update_guard as guard

S3 = "s3_infrastructuretype_ref"
AURORA = "aurora_postgres_infrastructuretype_ref"
K8S_PG = "devlift_k8s_postgres_infrastructuretype_ref"


class FakeRequest:
    def __init__(self, **kw):
        self.infrastructuretype_ref_code = kw.get("infra_type", S3)
        self.application_code = kw.get("application_code", "APP-1")
        self.environment = kw.get("environment", EnvironmentEnum.stage)
        self.geo_loc_mst_code = kw.get("geo_loc_mst_code", "region-aspora-mumbai")
        self.type_specific_config = kw.get("config", {})
        self.resource_group_mst_code = kw.get("resource_group_mst_code")


class FakeRow:
    def __init__(self, **kw):
        self.infrastructuretype_ref_code = kw.get("infra_type", S3)
        self.applications_mst_code = kw.get("applications_mst_code", "APP-1")
        self.environments_enum = kw.get("environments_enum", EnvironmentEnum.stage)
        self.geo_loc_mst_code = kw.get("geo_loc_mst_code", "region-aspora-mumbai")
        self.locator = kw.get("locator", {"identifier": "uploads"})
        self.status = kw.get("status", ResourceStatusEnum.ONLINE)
        self.deployment_status = kw.get("deployment_status")


@pytest.fixture
def deployed_bucket():
    return FakeRow()


class TestPlacementCannotMove:
    """Product, environment and region build the terragrunt path. Changing one
    writes a NEW file and leaves the old committed, so Terraform keeps managing
    the original resource and creates a second beside it."""

    def test_moving_to_another_environment_is_refused(self, deployed_bucket):
        request = FakeRequest(environment=EnvironmentEnum.prod)
        with pytest.raises(guard.InfrastructureUpdateError) as exc:
            guard.guard_update(request, deployed_bucket)
        assert "environment" in str(exc.value)

    def test_moving_to_another_product_is_refused(self, deployed_bucket):
        request = FakeRequest(application_code="APP-DIFFERENT")
        with pytest.raises(guard.InfrastructureUpdateError):
            guard.guard_update(request, deployed_bucket)

    def test_moving_to_another_region_is_refused(self, deployed_bucket):
        request = FakeRequest(geo_loc_mst_code="region-aspora-london")
        with pytest.raises(guard.InfrastructureUpdateError):
            guard.guard_update(request, deployed_bucket)

    def test_changing_the_resource_type_is_refused(self, deployed_bucket):
        """A bucket cannot become a queue; the row would keep S3 values under
        SQS semantics."""
        request = FakeRequest(infra_type="sqs_infrastructuretype_ref")
        with pytest.raises(guard.InfrastructureUpdateError):
            guard.guard_update(request, deployed_bucket)

    def test_every_change_is_named_not_just_the_first(self, deployed_bucket):
        request = FakeRequest(
            application_code="APP-2",
            environment=EnvironmentEnum.prod,
            geo_loc_mst_code="region-aspora-london",
        )
        with pytest.raises(guard.InfrastructureUpdateError) as exc:
            guard.guard_update(request, deployed_bucket)
        message = str(exc.value)
        for field in ("product", "environment", "region"):
            assert field in message

    def test_the_web_payload_passes(self, deployed_bucket):
        """The web sends the placement of the canvas the node already sits on,
        so a strict check must not break an ordinary settings save."""
        request = FakeRequest(config={"identifier": "uploads", "versioning": "true"})
        guard.guard_update(request, deployed_bucket)


class TestRenameAfterDeploy:
    """The path is keyed on the name, so a rename orphans the committed file and
    leaves the original resource live."""

    def test_renaming_a_deployed_resource_is_refused(self, deployed_bucket):
        request = FakeRequest(config={"identifier": "renamed"})
        with pytest.raises(guard.InfrastructureUpdateError) as exc:
            guard.guard_update(request, deployed_bucket)
        assert "rename" in str(exc.value).lower()

    def test_a_failed_deploy_still_counts_as_deployed(self):
        """A failed run can still have left a branch, a pull request or a
        committed file carrying the old name."""
        row = FakeRow(status=ResourceStatusEnum.FAILED)
        request = FakeRequest(config={"identifier": "renamed"})
        with pytest.raises(guard.InfrastructureUpdateError):
            guard.guard_update(request, row)

    def test_a_deployment_status_alone_counts_as_deployed(self):
        row = FakeRow(status=ResourceStatusEnum.DRAFT, deployment_status="creating_pr")
        request = FakeRequest(config={"identifier": "renamed"})
        with pytest.raises(guard.InfrastructureUpdateError):
            guard.guard_update(request, row)

    def test_resending_the_same_name_is_not_a_rename(self, deployed_bucket):
        request = FakeRequest(config={"identifier": "uploads"})
        guard.guard_update(request, deployed_bucket)

    def test_omitting_the_name_is_not_a_rename(self, deployed_bucket):
        request = FakeRequest(config={"versioning": "true"})
        guard.guard_update(request, deployed_bucket)

    def test_a_blank_name_is_treated_as_omitted(self, deployed_bucket):
        request = FakeRequest(config={"identifier": "   "})
        guard.guard_update(request, deployed_bucket)


class TestRenameBeforeDeploy:
    """The canvas writes the row when the node is dropped, before any name
    exists, so the first save that names it is not a rename."""

    def test_naming_a_row_that_has_no_name_yet_is_allowed(self):
        row = FakeRow(locator={}, status=ResourceStatusEnum.DRAFT)
        request = FakeRequest(config={"identifier": "first-name"})
        guard.guard_update(request, row)

    def test_renaming_an_undeployed_draft_is_allowed(self):
        """Matches the web, which greys the input out only once deployed."""
        row = FakeRow(locator={"identifier": "old"}, status=ResourceStatusEnum.DRAFT)
        request = FakeRequest(config={"identifier": "new"})
        guard.guard_update(request, row)


class TestNameFieldDiffersPerType:
    def test_aurora_stores_its_name_under_db_server_name(self):
        row = FakeRow(infra_type=AURORA, locator={"db_server_name": "orders-db"})
        request = FakeRequest(infra_type=AURORA, config={"db_server_name": "renamed"})
        with pytest.raises(guard.InfrastructureUpdateError):
            guard.guard_update(request, row)

    def test_k8s_postgres_accepts_either_input_key(self):
        """Its factory reads `identifier` or `server_name` but stores only
        `server_name`, so a rename sent under either key must be caught."""
        row = FakeRow(infra_type=K8S_PG, locator={"server_name": "pg-one"})
        for key in ("identifier", "server_name"):
            request = FakeRequest(infra_type=K8S_PG, config={key: "pg-two"})
            with pytest.raises(guard.InfrastructureUpdateError):
                guard.guard_update(request, row)

    def test_k8s_postgres_same_name_under_either_key_passes(self):
        row = FakeRow(infra_type=K8S_PG, locator={"server_name": "pg-one"})
        for key in ("identifier", "server_name"):
            guard.guard_update(
                FakeRequest(infra_type=K8S_PG, config={key: "pg-one"}), row
            )


class TestUnknownTypeFailsClosed:
    """A type added later without an entry would otherwise skip the rename check
    silently and reopen the hole."""

    def test_unknown_type_is_refused(self):
        row = FakeRow(infra_type="brand_new_infrastructuretype_ref")
        request = FakeRequest(infra_type="brand_new_infrastructuretype_ref")
        with pytest.raises(guard.InfrastructureUpdateError) as exc:
            guard.guard_update(request, row)
        assert "not supported" in str(exc.value)

    def test_every_routed_type_has_an_entry(self):
        """Keep this table in step with _build_infrastructure_factory_data."""
        routed = {
            "s3_infrastructuretype_ref",
            "sqs_infrastructuretype_ref",
            "dynamodb_infrastructuretype_ref",
            "devlift_k8s_postgres_infrastructuretype_ref",
            "elasticache_redis_infrastructuretype_ref",
            "aurora_postgres_infrastructuretype_ref",
            "aurora_mysql_infrastructuretype_ref",
        }
        assert routed == set(guard.NAME_FIELD_BY_TYPE)


class TestColumnsAreNotWiped:
    """BaseRepository.update writes every key it is handed, nulls included. The
    factory always emits these with create-time values."""

    def test_the_aws_identifier_is_not_cleared(self):
        cleaned = guard.strip_preserved_columns({
            "resource_identifier": None, "locator": {"identifier": "uploads"},
        })
        assert "resource_identifier" not in cleaned

    def test_the_workflow_link_is_not_cleared(self):
        assert "gitops_workflow_id" not in guard.strip_preserved_columns(
            {"gitops_workflow_id": None}
        )

    def test_a_live_resource_is_not_reset_to_initiated(self):
        cleaned = guard.strip_preserved_columns({
            "infra_status": "INITIATED",
            "infra_status_updated_at": "now",
            "infra_status_updated_by": "someone",
        })
        assert cleaned == {}

    def test_everything_else_survives(self):
        cleaned = guard.strip_preserved_columns({
            "locator": {"identifier": "uploads"},
            "name": "uploads",
            "applications_mst_code": "APP-1",
        })
        assert set(cleaned) == {"locator", "name", "applications_mst_code"}


class TestDeployedDetection:
    def test_a_clean_draft_has_not_deployed(self):
        assert not guard.has_deployed(FakeRow(status=ResourceStatusEnum.DRAFT))

    def test_a_row_with_no_status_has_not_deployed(self):
        assert not guard.has_deployed(FakeRow(status=None))

    @pytest.mark.parametrize("status", [
        ResourceStatusEnum.ONLINE,
        ResourceStatusEnum.PR_RAISED,
        ResourceStatusEnum.PROVISIONING,
        ResourceStatusEnum.FAILED,
    ])
    def test_anything_past_draft_has_deployed(self, status):
        assert guard.has_deployed(FakeRow(status=status))


class TestErrorShape:
    def test_refusal_becomes_a_400(self, deployed_bucket):
        request = FakeRequest(environment=EnvironmentEnum.prod)
        try:
            guard.guard_update(request, deployed_bucket)
        except guard.InfrastructureUpdateError as exc:
            http = guard.as_http_error(exc)
            assert isinstance(http, HTTPException)
            assert http.status_code == 400
        else:
            pytest.fail("expected a refusal")

    def test_the_message_says_what_to_do_instead(self, deployed_bucket):
        request = FakeRequest(environment=EnvironmentEnum.prod)
        with pytest.raises(guard.InfrastructureUpdateError) as exc:
            guard.guard_update(request, deployed_bucket)
        assert "Create a new resource" in str(exc.value)


SQS = "sqs_infrastructuretype_ref"


class TestFifoCannotFlip:
    """`fifo_queue` feeds resolve_sqs_name, so flipping it on a deployed queue
    repoints the row at a queue AWS does not have — a rename through a
    checkbox."""

    def test_turning_fifo_on_for_a_deployed_queue_is_refused(self):
        row = FakeRow(
            infra_type=SQS,
            locator={"identifier": "orders", "fifo_queue": False},
        )
        request = FakeRequest(
            infra_type=SQS,
            config={"identifier": "orders", "fifo_queue": True},
        )
        with pytest.raises(guard.InfrastructureUpdateError) as exc:
            guard.guard_update(request, row)
        assert "fifo_queue" in str(exc.value)

    def test_string_true_matching_stored_bool_is_not_a_change(self):
        """Form state sends "true"; the canvas sends True. Comparing raw would
        refuse a save nobody changed."""
        row = FakeRow(
            infra_type=SQS,
            locator={"identifier": "orders", "fifo_queue": True},
        )
        request = FakeRequest(
            infra_type=SQS,
            config={"identifier": "orders", "fifo_queue": "true"},
        )
        guard.guard_update(request, row)

    def test_a_draft_may_still_toggle_fifo(self):
        """Before deployment there is no queue to rename."""
        row = FakeRow(
            infra_type=SQS,
            status=ResourceStatusEnum.DRAFT,
            locator={"identifier": "orders", "fifo_queue": False},
        )
        request = FakeRequest(
            infra_type=SQS,
            config={"identifier": "orders", "fifo_queue": True},
        )
        guard.guard_update(request, row)

    def test_omitting_fifo_leaves_it_alone(self):
        row = FakeRow(
            infra_type=SQS,
            locator={"identifier": "orders", "fifo_queue": True},
        )
        request = FakeRequest(infra_type=SQS, config={"identifier": "orders"})
        guard.guard_update(request, row)


class TestNoWriteWhileDeploying:
    """A running deploy is reading this row. A write underneath it produces a
    resource matching neither the old config nor the new."""

    @pytest.mark.parametrize("status", [
        ResourceStatusEnum.INITIALISING,
        ResourceStatusEnum.PROVISIONING,
        ResourceStatusEnum.BUILDING,
        ResourceStatusEnum.DEPLOYING,
        ResourceStatusEnum.VERIFYING,
        ResourceStatusEnum.SOFT_DELETING,
        ResourceStatusEnum.HARD_DELETING,
    ])
    def test_in_flight_statuses_are_refused(self, status):
        row = FakeRow(status=status)
        request = FakeRequest(config={"identifier": "uploads"})
        with pytest.raises(guard.InfrastructureUpdateError):
            guard.guard_update(request, row)

    @pytest.mark.parametrize("status", [
        ResourceStatusEnum.DRAFT,
        ResourceStatusEnum.PR_RAISED,
        ResourceStatusEnum.ONLINE,
        ResourceStatusEnum.FAILED,
    ])
    def test_settled_statuses_are_allowed(self, status):
        """PR_RAISED included: nothing is running yet, and editing before merge
        is how a review comment gets addressed."""
        row = FakeRow(status=status)
        request = FakeRequest(config={"identifier": "uploads"})
        guard.guard_update(request, row)


REDIS = "elasticache_redis_infrastructuretype_ref"


class TestRenameUnderEveryAcceptedKey:
    """Each factory accepts more than one key for the name. The guard used to
    read only one of them per type, so a rename sent under the other key was
    invisible and went straight through."""

    def test_aurora_rename_sent_as_identifier_is_caught(self):
        row = FakeRow(infra_type=AURORA, locator={"db_server_name": "orders-db"})
        request = FakeRequest(infra_type=AURORA, config={"identifier": "renamed-db"})
        with pytest.raises(guard.InfrastructureUpdateError):
            guard.guard_update(request, row)

    def test_aurora_rename_sent_as_db_server_name_is_caught(self):
        row = FakeRow(infra_type=AURORA, locator={"db_server_name": "orders-db"})
        request = FakeRequest(infra_type=AURORA, config={"db_server_name": "renamed-db"})
        with pytest.raises(guard.InfrastructureUpdateError):
            guard.guard_update(request, row)

    def test_redis_rename_sent_as_cluster_name_is_caught(self):
        row = FakeRow(infra_type=REDIS, locator={"identifier": "cache"})
        request = FakeRequest(infra_type=REDIS, config={"redis_cluster_name": "other"})
        with pytest.raises(guard.InfrastructureUpdateError):
            guard.guard_update(request, row)

    def test_k8s_pg_resolves_the_key_the_factory_will_store(self):
        """The factory takes `identifier` first. Judging the payload on
        `server_name` would pass a request that stores something else."""
        row = FakeRow(infra_type=K8S_PG, locator={"server_name": "pg-one"})
        request = FakeRequest(
            infra_type=K8S_PG,
            config={"identifier": "pg-one", "server_name": "pg-two"},
        )
        guard.guard_update(request, row)

        renaming = FakeRequest(infra_type=K8S_PG, config={"identifier": "pg-three"})
        with pytest.raises(guard.InfrastructureUpdateError):
            guard.guard_update(renaming, row)


class TestFifoOnRowsPredatingTheKey:
    def test_a_locator_without_fifo_queue_does_not_block_a_save(self):
        """Nothing to compare against. Refusing would make every queue written
        before the key existed permanently unsavable."""
        row = FakeRow(infra_type=SQS, locator={"identifier": "orders"})
        request = FakeRequest(
            infra_type=SQS,
            config={"identifier": "orders", "fifo_queue": False},
        )
        guard.guard_update(request, row)


class TestLegacyRowsNamedUnderTheOtherKey:
    def test_a_k8s_pg_row_storing_identifier_is_still_protected(self):
        """The k8s postgres factory accepts `identifier` and stores
        `server_name`, so older rows can carry the name under either. Reading
        only the canonical key finds nothing on those, and a comparison against
        nothing lets every rename through."""
        row = FakeRow(infra_type=K8S_PG, locator={"identifier": "pg-one"})
        request = FakeRequest(infra_type=K8S_PG, config={"server_name": "renamed"})
        with pytest.raises(guard.InfrastructureUpdateError):
            guard.guard_update(request, row)
