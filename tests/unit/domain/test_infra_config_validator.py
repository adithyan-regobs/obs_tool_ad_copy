"""The naming and range rules every infrastructure write path now shares.

Before this module the rules lived in three places and covered two of the
paths: the SQS rules sat in a service the HTTP upsert never calls, the DynamoDB
rules sat on the HTTP route so Slack and MCP skipped them, and S3 had no
backend rule at all — only the dashboard's infraFieldRules.ts, which is a hint
rather than a guard.
"""

import pytest

from app.domain.validators import infra_config_validator as validator
from app.domain.validators.infra_config_validator import InfraConfigError

S3 = "s3_infrastructuretype_ref"
SQS = "sqs_infrastructuretype_ref"
DYNAMO = "dynamodb_infrastructuretype_ref"
REDIS = "elasticache_redis_infrastructuretype_ref"


class TestS3Names:
    """Enforced server-side for the first time — the API accepted anything."""

    @pytest.mark.parametrize("name", ["app-logs", "user.uploads", "a1b", "x" * 63])
    def test_valid_names_pass(self, name):
        validator.validate_config({"identifier": name}, S3, require_all=True)

    @pytest.mark.parametrize("name", [
        "My-Bucket",        # uppercase is not a legal bucket name
        "my bucket!",       # punctuation
        "ab",               # under the 3-char floor
        "x" * 64,           # over the 63-char ceiling
        "-leading",         # must start with a letter or number
        "trailing-",        # must end with one
    ])
    def test_invalid_names_are_refused(self, name):
        with pytest.raises(InfraConfigError):
            validator.validate_config({"identifier": name}, S3, require_all=True)

    def test_spaces_are_refused(self):
        """The dashboard rewrites them to dashes before sending; nothing on the
        server does, so a name with a space would reach AWS as-is."""
        with pytest.raises(InfraConfigError):
            validator.validate_config({"identifier": "app logs"}, S3, require_all=True)

    def test_cross_account_id_must_be_twelve_digits(self):
        with pytest.raises(InfraConfigError):
            validator.validate_config(
                {"identifier": "app-logs", "cross_account_account_id": "12345"},
                S3, require_all=True,
            )


class TestSqsNames:
    @pytest.mark.parametrize("name", ["order-events", "payment_processor", "x" * 80])
    def test_valid_names_pass(self, name):
        validator.validate_config({"identifier": name}, SQS, require_all=True)

    def test_fifo_suffix_is_refused(self):
        """resolve_sqs_name appends it, so a user-supplied one yields
        'name.fifo.fifo'."""
        with pytest.raises(InfraConfigError) as exc:
            validator.validate_config({"identifier": "orders.fifo"}, SQS, require_all=True)
        assert ".fifo" in str(exc.value)

    @pytest.mark.parametrize("name", ["x" * 81, "order events!", "orders/new"])
    def test_invalid_names_are_refused(self, name):
        with pytest.raises(InfraConfigError):
            validator.validate_config({"identifier": name}, SQS, require_all=True)


class TestSqsRanges:
    """AWS limits. Out of range means the terraform apply fails after the PR is
    merged, which is the expensive place to find out."""

    @pytest.mark.parametrize("field,value", [
        ("max_receive_count", 0),
        ("max_receive_count", 1001),
        ("visibility_timeout_seconds", -1),
        ("visibility_timeout_seconds", 43201),
        ("message_retention_seconds", 59),
        ("message_retention_seconds", 1209601),
        ("dlq_message_retention_seconds", 59),
    ])
    def test_out_of_range_is_refused(self, field, value):
        with pytest.raises(InfraConfigError):
            validator.validate_config({field: value}, SQS, require_all=False)

    @pytest.mark.parametrize("field,value", [
        ("max_receive_count", 1),
        ("max_receive_count", 1000),
        ("visibility_timeout_seconds", 0),
        ("visibility_timeout_seconds", 43200),
        ("message_retention_seconds", 60),
        ("message_retention_seconds", 1209600),
    ])
    def test_boundaries_pass(self, field, value):
        validator.validate_config({field: value}, SQS, require_all=False)

    def test_numeric_strings_from_form_state_are_accepted(self):
        validator.validate_config({"max_receive_count": "5"}, SQS, require_all=False)

    def test_non_numeric_text_is_refused(self):
        with pytest.raises(InfraConfigError):
            validator.validate_config({"max_receive_count": "five"}, SQS, require_all=False)

    def test_cross_account_ids_must_be_twelve_digits_and_unique(self):
        validator.validate_config(
            {"cross_account_ids": ["123456789012", "210987654321"]},
            SQS, require_all=False,
        )
        with pytest.raises(InfraConfigError):
            validator.validate_config(
                {"cross_account_ids": ["123456789012", "123456789012"]},
                SQS, require_all=False,
            )
        with pytest.raises(InfraConfigError):
            validator.validate_config(
                {"cross_account_ids": ["not-an-id"]}, SQS, require_all=False,
            )

    def test_blank_rows_from_the_array_editor_are_ignored(self):
        validator.validate_config(
            {"cross_account_ids": ["123456789012", "", "  "]},
            SQS, require_all=False,
        )


class TestDynamoNames:
    def test_the_raw_cap_leaves_room_for_the_prefix(self):
        """AWS caps the RESOLVED name at 255 and DevLift prefixes it with
        tenant, environment and region, so the raw cap is 200 — the number the
        dashboard already enforces."""
        validator.validate_config({"identifier": "x" * 200}, DYNAMO, require_all=False)
        with pytest.raises(InfraConfigError):
            validator.validate_config({"identifier": "x" * 201}, DYNAMO, require_all=False)

    def test_partition_key_charset(self):
        validator.validate_config({"partition_key": "userId"}, DYNAMO, require_all=False)
        with pytest.raises(InfraConfigError):
            validator.validate_config({"partition_key": "user id!"}, DYNAMO, require_all=False)

    def test_partition_key_type_must_be_s_n_or_b(self):
        validator.validate_config({"partition_key_type": "n"}, DYNAMO, require_all=False)
        with pytest.raises(InfraConfigError):
            validator.validate_config({"partition_key_type": "X"}, DYNAMO, require_all=False)


class TestPartialPayloads:
    """The canvas writes the row when a node is dropped, before the user has
    named anything. Requiring a full payload there fails the create and leaves a
    node the detail panel then locks."""

    def test_missing_fields_pass_when_not_required(self):
        validator.validate_config({}, S3, require_all=False)

    def test_missing_name_is_refused_when_required(self):
        with pytest.raises(InfraConfigError):
            validator.validate_config({}, S3, require_all=True)

    def test_explicit_null_is_a_clear_not_an_error(self):
        """The dashboard sends null to CLEAR a field and the script generators
        read it as 'remove this line'. Refusing nulls would break every clear."""
        validator.validate_config(
            {"identifier": None, "max_receive_count": None, "cross_account_ids": None},
            SQS, require_all=False,
        )

    def test_a_type_with_no_known_rules_passes(self):
        """Redis has no rules in this repo yet. Failing closed would block its
        creates outright, which nothing else validates either."""
        validator.validate_config({"identifier": "anything at all"}, REDIS, require_all=True)


class TestNameKeys:
    """Each type stores its name under its own locator key — comparing the wrong
    field would make the duplicate check silently useless."""

    @pytest.mark.parametrize("infra_type,key", [
        (S3, "identifier"),
        (SQS, "identifier"),
        ("devlift_k8s_postgres_infrastructuretype_ref", "server_name"),
        ("aurora_postgres_infrastructuretype_ref", "db_server_name"),
    ])
    def test_name_key_per_type(self, infra_type, key):
        assert validator.name_key_for(infra_type) == key

    def test_submitted_name_is_the_value_that_will_be_stored(self):
        assert validator.submitted_name({"identifier": "  app-logs "}, S3) == "app-logs"

    def test_submitted_name_is_none_when_absent(self):
        assert validator.submitted_name({}, S3) is None
        assert validator.submitted_name({"identifier": "  "}, S3) is None

    def test_unknown_type_has_no_name_key(self):
        assert validator.name_key_for("ec2_infrastructuretype_ref") is None


class TestIdentityKeysCannotBeSupplied:
    """Every factory spreads the leftover config LAST, so a supplied copy of a
    computed value lands on top of the one just derived."""

    def test_derived_s3_keys_are_dropped_from_the_passthrough(self):
        extra = validator.passthrough_extra(
            {"identifier": "uploads", "bucket_name": "someone-elses", "versioning": True},
            S3,
        )
        assert extra == {"versioning": True}

    def test_sqs_derived_urls_and_arns_are_dropped(self):
        extra = validator.passthrough_extra(
            {"identifier": "orders", "queue_url": "https://evil", "create_dlq": True},
            SQS,
        )
        assert extra == {"create_dlq": True}

    def test_alternate_name_keys_are_dropped_too(self):
        extra = validator.passthrough_extra(
            {"identifier": "pg-one", "server_name": "pg-two"},
            "devlift_k8s_postgres_infrastructuretype_ref",
        )
        assert extra == {}


class TestNameResolutionMatchesTheFactory:
    def test_k8s_pg_prefers_identifier_like_its_factory(self):
        name = validator.submitted_name(
            {"identifier": "pg-one", "server_name": "pg-two"},
            "devlift_k8s_postgres_infrastructuretype_ref",
        )
        assert name == "pg-one"

    def test_aurora_prefers_db_server_name_like_its_factory(self):
        name = validator.submitted_name(
            {"identifier": "ignored", "db_server_name": "orders-db"},
            "aurora_postgres_infrastructuretype_ref",
        )
        assert name == "orders-db"

    def test_redis_falls_back_to_the_cluster_name_key(self):
        assert validator.submitted_name({"redis_cluster_name": "cache"}, REDIS) == "cache"


class TestNamesAlreadyStored:
    def test_an_unchanged_name_skips_the_syntax_check(self):
        """Rows predate these rules. Re-validating a name the request is not
        changing would make such a row permanently unsavable."""
        validator.validate_config(
            {"identifier": "Legacy_Bucket"}, S3,
            require_all=False, existing_name="Legacy_Bucket",
        )

    def test_changing_it_to_something_invalid_is_still_refused(self):
        with pytest.raises(InfraConfigError):
            validator.validate_config(
                {"identifier": "Another_Bad_One"}, S3,
                require_all=False, existing_name="Legacy_Bucket",
            )


class TestSurroundingSpace:
    def test_a_leading_space_is_refused_not_trimmed(self):
        """The factory stores the raw value, so trimming here would validate one
        string and store another."""
        with pytest.raises(InfraConfigError):
            validator.validate_config({"identifier": " orders"}, SQS, require_all=False)


class TestAccountIdIsAnInput:
    """It cannot be derived — the dashboard sends the caller's account
    (useInfraResourceConfigHydration.ts:701) and the factory reads it to build
    the queue ARN. Stripping it silently moved every ARN to the default
    account."""

    def test_account_id_survives_the_passthrough(self):
        extra = validator.passthrough_extra(
            {"identifier": "orders", "accountId": "123456789012"}, SQS,
        )
        assert extra == {"accountId": "123456789012"}

    def test_the_arn_itself_is_still_protected(self):
        extra = validator.passthrough_extra(
            {"identifier": "orders", "queue_arn": "arn:aws:sqs:::evil"}, SQS,
        )
        assert extra == {}


class TestWhitespaceOnlyNames:
    @pytest.mark.parametrize("infra_type", [S3, SQS, "devlift_k8s_postgres_infrastructuretype_ref"])
    def test_a_name_of_only_spaces_is_refused(self, infra_type):
        """It strips to empty, so it reads as "no name sent" — but the factories
        treat it as truthy and store it, naming the resource with spaces."""
        with pytest.raises(InfraConfigError):
            validator.validate_config({"identifier": "   "}, infra_type, require_all=False)


class TestSettingTheNameTheFactoryWillRead:
    def test_other_name_keys_are_dropped_not_just_overwritten(self):
        """k8s postgres reads `identifier` before `server_name`, so leaving a
        junk `identifier` in place would beat the name being set."""
        out = validator.with_name(
            {"identifier": "   ", "cpu": "2"},
            "devlift_k8s_postgres_infrastructuretype_ref",
            "pg-one",
        )
        assert out == {"identifier": "pg-one", "cpu": "2"}

    def test_aurora_gets_its_own_preferred_key(self):
        out = validator.with_name({}, "aurora_postgres_infrastructuretype_ref", "orders-db")
        assert out == {"db_server_name": "orders-db"}


class TestTheNameAwsActuallySees:
    """The typed name is not the name AWS gets. The terragrunt writes the raw
    identifier and terraform prefixes it with tenant/environment/region, so a
    name inside the AWS limit can still overflow it — and it fails at apply,
    after the pull request is merged."""

    def _s3_locator(self, typed):
        from app.core.enum import EnvironmentEnum
        from app.domain.factories import infrastructure_mst_factory as factory
        return factory.make_infrastructure_mst_s3(
            config={"identifier": typed},
            tenant_code="aspora", application_code="APP",
            environment=EnvironmentEnum.stage, region="ap-south-1",
            infrastructuretype_ref_code=S3,
            infra_vendor_accounts_mst_code="acc",
        )["locator"]

    def test_a_name_that_fits_after_prefixing_passes(self):
        locator = self._s3_locator("a" * 38)
        assert len(locator["bucket_name"]) == 63
        validator.validate_resolved_names(locator, S3)

    def test_one_character_more_is_refused(self):
        with pytest.raises(InfraConfigError) as exc:
            validator.validate_resolved_names(self._s3_locator("a" * 39), S3)
        assert "63" in str(exc.value)

    def test_a_name_at_the_raw_aws_limit_is_still_refused(self):
        """63 is the AWS limit for the FINAL name, so 63 typed is always over
        it. The old rule accepted exactly this."""
        with pytest.raises(InfraConfigError):
            validator.validate_resolved_names(self._s3_locator("a" * 63), S3)

    def test_the_message_says_how_many_characters_are_left(self):
        """A bare "too long" sends the user guessing; the budget depends on the
        tenant, environment and type."""
        with pytest.raises(InfraConfigError) as exc:
            validator.validate_resolved_names(self._s3_locator("a" * 50), S3)
        assert "38 characters or fewer" in str(exc.value)

    def test_the_dlq_name_is_checked_too(self):
        """The DLQ carries a '-dlq' suffix on top of everything else, so it goes
        over the limit before the queue itself does."""
        from app.core.enum import EnvironmentEnum
        from app.domain.factories import infrastructure_mst_factory as factory

        def locator(create_dlq):
            return factory.make_infrastructure_mst_sqs(
                config={"identifier": "b" * 71, "fifo_queue": True,
                        "create_dlq": create_dlq},
                tenant_code="aspora", application_code="APP",
                environment=EnvironmentEnum.stage, region="ap-south-1",
                infrastructuretype_ref_code=SQS,
                infra_vendor_accounts_mst_code="acc",
            )["locator"]

        validator.validate_resolved_names(locator(False), SQS)
        with pytest.raises(InfraConfigError):
            validator.validate_resolved_names(locator(True), SQS)

    def test_a_type_with_no_limit_passes(self):
        validator.validate_resolved_names({"identifier": "x" * 300}, REDIS)
