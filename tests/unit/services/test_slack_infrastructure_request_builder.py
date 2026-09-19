from app.services.slack.shared.infrastructure_request_builder import (
    is_v2_text_placement_resource,
)


def test_is_v2_text_placement_resource_handles_s3_variants() -> None:
    assert is_v2_text_placement_resource("s3")
    assert is_v2_text_placement_resource("S3")
    assert is_v2_text_placement_resource("s3_infrastructuretype_ref")
    assert is_v2_text_placement_resource(" S3_INFRASTRUCTURETYPE_REF ")


def test_is_v2_text_placement_resource_handles_sqs_variants() -> None:
    assert is_v2_text_placement_resource("sqs")
    assert is_v2_text_placement_resource("sqs_infrastructuretype_ref")


def test_is_v2_text_placement_resource_rejects_non_text_resources() -> None:
    assert not is_v2_text_placement_resource("")
    assert not is_v2_text_placement_resource("database_infrastructuretype_ref")


def test_is_v2_text_placement_resource_handles_dynamodb_variants() -> None:
    assert is_v2_text_placement_resource("dynamodb")
    assert is_v2_text_placement_resource("dynamo")
    assert is_v2_text_placement_resource("dynamodb_infrastructuretype_ref")
