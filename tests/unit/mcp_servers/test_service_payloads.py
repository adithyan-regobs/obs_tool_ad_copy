"""Pure tests for the EKS payload assembly (no I/O).

The fixture is the exact `isReady` payload the chatbot returned for the
`eks_service_form` during the 2026-09-11 dry run (after the chatbot-side
fixes: real booleans, no `/*` default, build_args as a list).
"""

import copy

import pytest

from app.mcp_servers.devlift_mcp import service_payloads as sp


CACHED = {
    "kind": "service",
    "form_id": "eks_service_form",
    "create_service": {
        "application_code": "d19899af-78e8-44aa-b95f-afd932a019e3",
        "resource_group_code": "154a9547-93aa-4589-baa7-402bc6d04288",
        "service_name": "mcp-dryrun-api",
        "service_type": "API",
    },
    "service_config": {
        "infrastructuretype_ref_code": "eks_infrastructuretype_ref",
        "infra_vendor_enum": "aws",
        "environment": "stage",
        "geo_loc_mst_code": "region-aspora-mumbai",
        "language_ref_code": "PYTHON_3_10",
        "language_name": "Python",
        "language_version": "3.10",
        "config": {
            "repository": "Regobs/chat-bot-POC",
            "branches": ["main"],
            "cpu_requested": "0.5",
            "cpu_limit": "1",
            "memory_requested": "1",
            "memory_limit": "2",
            "port": "8080",
            "health": "/health",
            "service_path": "/api",
            "alb_schema": "internal",
            "compute": "on-demand",
            "custom_iam_policies": [],
            "hpa": {"enabled": False, "min_replicas": None, "max_replicas": None},
            "replica_count": "1",
            "create_ecr": True,
            "create_secrets": True,
            "create_ssm": True,
            "create_argo": True,
            "auth_mode": "pod_identity",
            "generate_dockerfile": True,
            "dockerfile_path": "Dockerfile",
            "xms": None,
            "xmx": None,
            "go_config_path": None,
            "go_use_aws_secrets": False,
            "build_args": [{"name": "APP_ENV", "value": "prod"}],
            "build_path": None,
            "other_paths": None,
        },
    },
    "collected_data": {
        "product": "core",
        "language": "Python",
        "version": "Python 3.10",
        "service_name": "mcp-dryrun-api",
    },
}

LOCATOR_CAMEL = {
    "cluster_name": "eks-stage",
    "cluster_arn": "arn:aws:eks:ap-south-1:1:cluster/eks-stage",
    "cloudRegion": "ap-south-1",
    "cloudRegionId": "cr-1-ap-south-1",
    "subnetIds": ["subnet-a", "subnet-b"],
    "vpcId": "vpc-1",
}


def test_create_service_payload_matches_create_service_request():
    payload = sp.build_create_service_payload(CACHED)
    assert payload == {
        "application_code": "d19899af-78e8-44aa-b95f-afd932a019e3",
        "resource_group_code": "154a9547-93aa-4589-baa7-402bc6d04288",
        "service_name": "mcp-dryrun-api",
        "service_type": "API",
        "is_active": True,
        "is_public_facing": False,
    }


def test_create_service_payload_requires_name():
    broken = copy.deepcopy(CACHED)
    broken["create_service"]["service_name"] = ""
    with pytest.raises(sp.ServicePayloadError):
        sp.build_create_service_payload(broken)


def test_baseline_payload_carries_only_creation_context():
    payload = sp.build_baseline_config_payload(
        CACHED,
        services_mst_code="svc-1",
        infrastructure_mst_code="infra-eks-1",
        cluster_locator=LOCATOR_CAMEL,
    )
    assert payload["services_mst_code"] == "svc-1"
    assert payload["infrastructure_mst_code"] == "infra-eks-1"
    assert payload["infrastructuretype_ref_code"] == "eks_infrastructuretype_ref"
    assert payload["infra_vendor_enum"] == "aws"
    assert payload["environment"] == "stage"
    assert payload["geo_loc_mst_code"] == "region-aspora-mumbai"
    assert "language_ref_code" not in payload  # web leaves it empty at creation; the draft carries it
    assert payload["config"] == {
        "alb_selection": "existing_alb",
        "namespace": "mcp-dryrun-api-service",
        "cluster_name": "eks-stage",
        "cluster_arn": "arn:aws:eks:ap-south-1:1:cluster/eks-stage",
        "region": "ap-south-1",
        "cloud_region_id": "cr-1-ap-south-1",
        "subnet_ids": ["subnet-a", "subnet-b"],
    }
    # None of the user's settings leak into the live baseline row.
    for key in ("repository", "cpu_requested", "port", "replica_count", "branches"):
        assert key not in payload["config"]


def test_baseline_payload_background_service_gets_no_alb_and_snake_locator():
    cached = copy.deepcopy(CACHED)
    cached["create_service"]["service_type"] = "BACKGROUND_SERVICE"
    cached["create_service"]["service_name"] = "worker-service"
    locator = {
        "cluster_name": "eks-stage",
        "cluster_arn": "arn",
        "region": "ap-south-1",
        "cloud_region_id": "cr-x",
        "subnet_ids": ["s1"],
        "vpc_id": "vpc-9",
    }
    payload = sp.build_baseline_config_payload(
        cached, services_mst_code="svc", infrastructure_mst_code="infra", cluster_locator=locator
    )
    assert payload["config"]["alb_selection"] == "no_alb"
    assert payload["config"]["namespace"] == "worker-service"
    assert payload["config"]["region"] == "ap-south-1"
    assert payload["config"]["subnet_ids"] == ["s1"]
    assert "vpc_id" not in payload["config"]  # web never sets it at creation


def test_baseline_payload_without_cluster_omits_cluster_keys():
    payload = sp.build_baseline_config_payload(
        CACHED, services_mst_code="svc", infrastructure_mst_code=None, cluster_locator=None
    )
    assert "infrastructure_mst_code" not in payload
    assert payload["config"] == {"alb_selection": "existing_alb", "namespace": "mcp-dryrun-api-service"}


def test_draft_snapshot_passes_config_through_and_adds_identity():
    snapshot = sp.build_draft_snapshot(
        CACHED, services_mst_code="svc-1", infrastructure_mst_code="infra-eks-1", ingress_group_order=65
    )
    config = snapshot["config"]
    # Chatbot values are canonical: passed through untouched, None keys dropped.
    assert config["cpu_requested"] == "0.5"
    assert config["memory_limit"] == "2"
    assert config["port"] == "8080"
    assert config["hpa"] == {"enabled": False, "min_replicas": None, "max_replicas": None}
    assert config["build_args"] == [{"name": "APP_ENV", "value": "prod"}]
    assert config["generate_dockerfile"] is True
    assert "xms" not in config and "build_path" not in config and "other_paths" not in config
    # Identity at the root, where the queue readers look.
    assert snapshot["services_mst_code"] == "svc-1"
    assert snapshot["service_name"] == "mcp-dryrun-api"
    assert snapshot["service_type"] == "API"
    assert snapshot["environment"] == "stage"
    assert snapshot["geo_loc_mst_code"] == "region-aspora-mumbai"
    assert snapshot["infrastructuretype_ref_code"] == "eks_infrastructuretype_ref"
    assert snapshot["infrastructure_mst_code"] == "infra-eks-1"
    assert snapshot["applications_mst_code"] == "d19899af-78e8-44aa-b95f-afd932a019e3"
    assert snapshot["product_name"] == "core"
    assert snapshot["language_name"] == "Python"
    assert snapshot["language_version"] == "3.10"  # bare number from the template, not the label
    # The CODE lives inside `config`, where compute_changes walks and where the
    # web's Settings tab puts it. At the root it relied on the COLUMN_FIELDS
    # lift instead, and a language change saved through this tool produced a
    # draft whose diff showed no language at all — while the same change made
    # from the dashboard diffed normally.
    assert config["language_ref_code"] == "PYTHON_3_10"
    assert "language_ref_code" not in snapshot
    # ... but the display copies stay at the root: the live config never stores
    # them, so inside `config` they would diff as changes nobody made.
    assert "language_name" not in config and "language_version" not in config
    assert snapshot["ingress_group_order"] == 65


def test_draft_snapshot_without_ingress_order_omits_key():
    snapshot = sp.build_draft_snapshot(CACHED, services_mst_code="svc", infrastructure_mst_code="infra")
    assert "ingress_group_order" not in snapshot


@pytest.mark.parametrize(
    "name,expected",
    [("demo", "demo-service"), ("demo-service", "demo-service"), ("  api ", "api-service")],
)
def test_eks_namespace_rule(name, expected):
    assert sp.eks_namespace_for(name) == expected


def test_a_language_that_never_resolved_is_kept_out_of_the_config():
    """The chatbot resolves language_ref_code from the chosen version option's
    `value`, so an empty option list makes it fall through to the raw LABEL —
    '1.23' where 'GO_1_23' was meant. It still reaches the snapshot; the
    handler is what refuses it, because nothing downstream would: the draft
    saves, submit and approve pass, and the value finally fails the
    service_configs foreign key inside a Temporal activity, after the PR."""
    cached = {
        **CACHED,
        "service_config": {**CACHED["service_config"], "language_ref_code": "1.23"},
    }
    snapshot = sp.build_draft_snapshot(
        cached, services_mst_code="svc", infrastructure_mst_code="infra"
    )
    # It is carried where a real code would go — so the guard has something to
    # check, and a future reader can see the bogus value rather than a silence.
    assert snapshot["config"]["language_ref_code"] == "1.23"
