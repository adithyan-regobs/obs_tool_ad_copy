"""What the resource edit tools will and will not let the LLM do.

The rules that matter here are the ones a user can reach by asking: "change
versioning AND move it to prod". The move must be refused in words, not
silently dropped, and it must never reach the database.
"""

import pytest

from app.mcp_servers.devlift_mcp import resource_edit as redit

S3 = "s3_infrastructuretype_ref"
SQS = "sqs_infrastructuretype_ref"


class TestWhatCanChange:
    def test_s3_offers_only_what_the_generator_patches(self):
        """A field outside this set produces a byte-identical terragrunt, and an
        empty diff fails PR creation on the enterprise path."""
        assert [f["field"] for f in redit.editable_fields(S3)] == [
            "versioning", "enable_s3_replication", "cross_account_account_id",
        ]

    def test_sqs_offers_the_tuning_fields(self):
        fields = [f["field"] for f in redit.editable_fields(SQS)]
        assert "visibility_timeout_seconds" in fields
        assert "create_dlq" in fields

    def test_every_field_shown_to_a_user_has_a_human_label(self):
        for infra_type in redit.supported_infra_types():
            shown = [f["field"] for f in redit.editable_fields(infra_type)]
            shown += list(redit.locked_fields(infra_type))
            for field in shown:
                assert redit.label_for(field) != field, field

    def test_dynamodb_is_not_editable(self):
        """A DynamoDB table cannot be updated in place — an edit would have to
        destroy and recreate it."""
        assert not redit.is_supported("dynamodb_infrastructuretype_ref")
        assert redit.editable_fields("dynamodb_infrastructuretype_ref") == ()


class TestWhatCannotChange:
    def test_the_name_and_everything_derived_from_it_are_locked(self):
        locked = redit.locked_fields(S3)
        assert "identifier" in locked
        assert "bucket_name" in locked
        assert "bucket_arn" in locked

    def test_fifo_is_locked_because_it_renames_the_queue(self):
        assert "fifo_queue" in redit.locked_fields(SQS)

    def test_locked_and_editable_never_overlap(self):
        for infra_type in redit.supported_infra_types():
            editable = {f["field"] for f in redit.editable_fields(infra_type)}
            assert not editable & set(redit.locked_fields(infra_type))

    def test_a_locked_field_is_refused_with_its_own_reason(self):
        refused = redit.refused_changes({"identifier": "renamed"}, S3)
        assert refused["locked"] == ["identifier"]
        assert refused["placement"] == [] and refused["unknown"] == []

    def test_placement_is_not_a_field_at_all(self):
        """Neither tool takes an environment, so the LLM can only smuggle it in
        through `changes` — where it is refused, and refused as a MOVE so the
        user hears why rather than "not a field"."""
        refused = redit.refused_changes({"environment": "prod"}, S3)
        assert refused["placement"] == ["environment"]
        assert refused["unknown"] == []

    @pytest.mark.parametrize("key", ["environment", "product", "region", "geo_loc_mst_code"])
    def test_every_placement_word_is_caught(self, key):
        assert redit.refused_changes({key: "x"}, S3)["placement"] == [key]

    def test_a_mixed_request_refuses_only_the_bad_field(self):
        """'change versioning AND move it to prod' — the versioning half is
        still a valid change; the move is not."""
        refused = redit.refused_changes(
            {"versioning": True, "environment": "prod"}, S3,
        )
        assert refused["placement"] == ["environment"]
        assert refused["locked"] == [] and refused["unknown"] == []


class TestTheDiffShownBeforeWriting:
    def test_only_real_changes_are_listed(self):
        rows = redit.diff_rows(
            {"versioning": False, "enable_s3_replication": False},
            {"versioning": True, "enable_s3_replication": False},
        )
        assert [r["field"] for r in rows] == ["versioning"]

    def test_a_string_true_matching_a_stored_bool_is_not_a_change(self):
        """Form state writes "true", the canvas writes True. Comparing raw would
        show a change nobody made."""
        assert redit.diff_rows({"versioning": True}, {"versioning": "true"}) == []

    def test_numbers_compare_across_string_and_int(self):
        assert redit.diff_rows(
            {"max_receive_count": 5}, {"max_receive_count": "5"},
        ) == []

    def test_rows_carry_the_label_not_the_field_name(self):
        rows = redit.diff_rows({"versioning": False}, {"versioning": True})
        assert rows[0]["label"] == "Object versioning"
        assert rows[0]["from"] is False and rows[0]["to"] is True


class TestTheConfigSentToTheBackend:
    def test_the_full_stored_set_is_sent_not_just_the_changes(self):
        """The factory defaults anything missing — for a queue, an absent
        fifo_queue reads as False and rebuilds the name without its suffix,
        pointing the row at a queue that does not exist."""
        config = redit.build_update_config(
            {"identifier": "orders", "fifo_queue": True, "create_dlq": False},
            {"create_dlq": True},
        )
        assert config["identifier"] == "orders"
        assert config["fifo_queue"] is True
        assert config["create_dlq"] is True

    def test_the_stored_locator_is_not_mutated(self):
        stored = {"versioning": False}
        redit.build_update_config(stored, {"versioning": True})
        assert stored == {"versioning": False}


class TestTenantGate:
    def test_only_patching_tenants_are_supported(self):
        """Elsewhere the generator rewrites the whole file, so a second deploy
        would drop every hand-added input."""
        assert "aspora" in redit.SUPPORTED_TENANTS
        assert "vance" not in redit.SUPPORTED_TENANTS


class TestSomeoneElseEditedMeanwhile:
    """An edit session caches the settings for six hours. Building the write on
    that copy puts every stale value back, undoing a change made in DevLift
    meanwhile — with nothing in the diff to show for it."""

    OPENED = {"visibility_timeout_seconds": 30, "create_dlq": True}

    def test_a_field_only_they_changed_is_not_a_conflict(self):
        moved = redit.concurrent_changes(
            self.OPENED,
            {"visibility_timeout_seconds": 300, "create_dlq": True},
            {"create_dlq": False},
        )
        assert moved["collisions"] == []
        assert [f["field"] for f in moved["other"]] == ["visibility_timeout_seconds"]

    def test_a_field_both_sides_changed_is_a_conflict(self):
        moved = redit.concurrent_changes(
            self.OPENED,
            {"visibility_timeout_seconds": 300, "create_dlq": True},
            {"visibility_timeout_seconds": 60},
        )
        assert moved["other"] == []
        conflict = moved["collisions"][0]
        assert conflict["when_opened"] == 30
        assert conflict["now"] == 300
        assert conflict["you_want"] == 60

    def test_nothing_moved_means_nothing_to_report(self):
        moved = redit.concurrent_changes(self.OPENED, dict(self.OPENED), {"create_dlq": False})
        assert moved == {"collisions": [], "other": []}

    def test_string_and_bool_forms_are_not_a_phantom_conflict(self):
        """The locator holds booleans as real bools from the canvas and as
        "true"/"false" from a form. Comparing raw would raise a conflict over a
        value nobody touched."""
        moved = redit.concurrent_changes(
            {"create_dlq": True}, {"create_dlq": "true"}, {"create_dlq": False},
        )
        assert moved["collisions"] == []

    def test_a_field_added_since_the_session_opened_is_reported(self):
        moved = redit.concurrent_changes(
            {}, {"max_receive_count": 5}, {"create_dlq": True},
        )
        assert [f["field"] for f in moved["other"]] == ["max_receive_count"]

    def test_the_diff_is_measured_against_the_current_values(self):
        """Not against the cached copy — otherwise the preview shows a change
        that is already applied, or hides one that is not."""
        rows = redit.diff_rows({"visibility_timeout_seconds": 300},
                               {"visibility_timeout_seconds": 300})
        assert rows == []
