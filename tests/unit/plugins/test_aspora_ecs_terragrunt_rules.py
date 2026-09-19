import re
from pathlib import Path

import pytest

from app.plugin.aspora.script_gen_components.aspora_ecs_terragrunt_script_gen_component import (
    AsporaEcsTerragruntScriptGenComponent,
)


TEMPLATE_DIR = Path(__file__).resolve().parents[3] / "templates" / "terragrunt" / "services"


def _load_template(name: str) -> str:
    return (TEMPLATE_DIR / name).read_text()


def _comment_field(content: str, field: str) -> str:
    pattern = rf'^(\s*)({field}\s*=\s*[^\n]+)$'
    return re.sub(pattern, r"\1// \2", content, flags=re.MULTILINE)


def _uncomment_field(content: str, field: str) -> str:
    pattern = rf'^(\s*)(#|//)\s*({field}\s*=\s*[^\n]+)$'
    return re.sub(pattern, r"\1\3", content, flags=re.MULTILINE)


def _is_commented(content: str, field: str) -> bool:
    return re.search(rf'^(\s*)(#|//)\s*{field}\s*=', content, re.MULTILINE) is not None


def _is_uncommented(content: str, field: str) -> bool:
    return re.search(rf'^(\s*){field}\s*=', content, re.MULTILINE) is not None


def _is_commented_with_fragment(content: str, field: str, fragment: str) -> bool:
    pattern = rf'^(\s*)(#|//)\s*{field}\s*=\s*.*{re.escape(fragment)}.*$'
    return re.search(pattern, content, re.MULTILINE) is not None


def _is_uncommented_with_fragment(content: str, field: str, fragment: str) -> bool:
    pattern = rf'^(\s*){field}\s*=\s*.*{re.escape(fragment)}.*$'
    return re.search(pattern, content, re.MULTILINE) is not None


@pytest.mark.parametrize(
    "environment,should_comment",
    [
        ("prod", False),
        ("stage", True),
        ("staging", True),
        ("dev", True),
        ("qa", True),
    ],
)
def test_priority_alarms_commenting(environment, should_comment):
    component = AsporaEcsTerragruntScriptGenComponent()
    content = _load_template("api-common-alb.hcl")
    fields = [
        "devops_p0_alarm_sns_topic_arn",
        "devops_p1_alarm_sns_topic_arn",
        "devs_p0_alarm_sns_topic_arn",
        "devs_p1_alarm_sns_topic_arn",
    ]

    if environment == "prod":
        for field in fields:
            content = _comment_field(content, field)

    updated = component._apply_environment_rules(
        content,
        environment=environment,
        product_name="core",
        tenant="test",
        field_mapping={},
        service_type="API",
    )

    for field in fields:
        assert _is_commented_with_fragment(
            updated,
            field,
            "dependency.slack_ops.outputs",
        ) == should_comment
        if not should_comment:
            assert _is_uncommented_with_fragment(
                updated,
                field,
                "dependency.slack_ops.outputs",
            )


@pytest.mark.parametrize(
    "environment,expected_create_alarms,expected_alb_suffix_commented",
    [
        ("prod", "true", False),
        ("dev", "false", True),
        ("stage", "false", True),
    ],
)
def test_create_alarms_and_alb_suffix_rules(
    environment,
    expected_create_alarms,
    expected_alb_suffix_commented,
):
    component = AsporaEcsTerragruntScriptGenComponent()
    content = _load_template("api-common-alb.hcl")

    if environment == "prod":
        content = re.sub(
            r"create_alarms\s*=\s*true",
            "create_alarms = false",
            content,
        )
        content = _comment_field(content, "alb_arn_suffix")
    else:
        content = _uncomment_field(content, "alb_arn_suffix")

    updated = component._apply_environment_rules(
        content,
        environment=environment,
        product_name="core",
        tenant="test",
        field_mapping={},
        service_type="API",
    )

    assert re.search(
        rf"create_alarms\s*=\s*{expected_create_alarms}",
        updated,
        re.MULTILINE,
    )
    assert _is_commented(updated, "alb_arn_suffix") == expected_alb_suffix_commented


@pytest.mark.parametrize(
    "environment,product,service_type,should_comment",
    [
        ("stage", "core", "API", True),
        ("staging", "core", "API", True),
        ("prod", "falcon", "API", True),
        ("prod", "core", "API", False),
        ("dev", "core", "API", False),
        ("qa", "core", "API", False),
        ("stage", "falcon", "OPS_TOOLS", False),
    ],
)
def test_capacity_provider_rule(
    environment,
    product,
    service_type,
    should_comment,
):
    component = AsporaEcsTerragruntScriptGenComponent()
    content = _load_template("api-common-alb.hcl")
    content = _comment_field(content, "capacity_provider_name")

    updated = component._apply_environment_rules(
        content,
        environment=environment,
        product_name=product,
        tenant="test",
        field_mapping={},
        service_type=service_type,
    )

    assert _is_commented_with_fragment(
        updated,
        "capacity_provider_name",
        "dependency.common_infra.outputs.capacity_provider_name",
    ) == should_comment
    if not should_comment:
        assert _is_uncommented_with_fragment(
            updated,
            "capacity_provider_name",
            "dependency.common_infra.outputs.capacity_provider_name",
        )


@pytest.mark.parametrize(
    "environment,starting_value,expected_value",
    [
        ("dev", "true", "false"),
        ("stage", "false", "true"),
    ],
)
def test_enable_datadog_sidecar_rule(environment, starting_value, expected_value):
    component = AsporaEcsTerragruntScriptGenComponent()
    content = _load_template("api-common-alb.hcl")
    content = re.sub(
        r"enable_datadog_sidecar\s*=\s*\w+",
        f"enable_datadog_sidecar = {starting_value}",
        content,
    )

    updated = component._apply_environment_rules(
        content,
        environment=environment,
        product_name="core",
        tenant="test",
        field_mapping={},
        service_type="API",
    )

    assert re.search(
        rf"enable_datadog_sidecar\s*=\s*{expected_value}",
        updated,
        re.MULTILINE,
    )


def test_enable_datadog_sidecar_skips_when_api_value_present():
    component = AsporaEcsTerragruntScriptGenComponent()
    content = _load_template("api-common-alb.hcl")
    content = re.sub(
        r"enable_datadog_sidecar\s*=\s*\w+",
        "enable_datadog_sidecar = false",
        content,
    )

    updated = component._apply_environment_rules(
        content,
        environment="dev",
        product_name="core",
        tenant="test",
        field_mapping={"enable_datadog_sidecar": "true"},
        service_type="API",
    )

    assert re.search(
        r"enable_datadog_sidecar\s*=\s*false",
        updated,
        re.MULTILINE,
    )


def test_http_scaling_uncomments_existing_alb_arn():
    component = AsporaEcsTerragruntScriptGenComponent()
    content = _load_template("api-common-alb.hcl")

    updated = component._apply_http_scaling_alb_arn_rule(
        content,
        {"http_scaling_enabled": "true"},
    )

    assert _is_uncommented(updated, "existing_alb_arn")


def test_http_scaling_comments_existing_alb_arn():
    component = AsporaEcsTerragruntScriptGenComponent()
    content = _load_template("api-common-alb.hcl")
    content = _uncomment_field(content, "existing_alb_arn")

    updated = component._apply_http_scaling_alb_arn_rule(
        content,
        {"http_scaling_enabled": "false"},
    )

    assert _is_commented(updated, "existing_alb_arn")


def test_env_file_paths_update_standard_service_with_hash_comments():
    component = AsporaEcsTerragruntScriptGenComponent()
    content = _load_template("api-common-alb.hcl")
    content = content.replace("// service_container_configs", "# service_container_configs")
    content = content.replace("// service_container_secrets", "# service_container_secrets")

    updated = component._uncomment_env_file_paths(
        content,
        service_name="casa",
        environment="dev",
        tenant="vance",
        product_name="core",
        service_type="API",
    )

    assert "../../envs-dev/casa-service/non-secure/casa-service-configs.json" in updated
    assert "../../envs-dev/casa-service/secure/casa-service-secrets.json" in updated
    assert "# service_container_configs" not in updated
    assert "# service_container_secrets" not in updated


def test_env_file_paths_update_ops_tools_service():
    component = AsporaEcsTerragruntScriptGenComponent()
    content = _load_template("ops-tools.hcl")

    updated = component._uncomment_env_file_paths(
        content,
        service_name="ops",
        environment="dev",
        tenant="vance",
        product_name="core",
        service_type="OPS_TOOLS",
    )

    assert "../../envs-dev/dev-tools/non-secure/ops-service-configs.json" in updated
    assert "../../envs-dev/dev-tools/secure/ops-service-secrets.json" in updated


def test_env_file_paths_skip_falcon_services():
    component = AsporaEcsTerragruntScriptGenComponent()
    content = _load_template("api-common-alb.hcl")

    updated = component._uncomment_env_file_paths(
        content,
        service_name="falcon-api",
        environment="prod",
        tenant="vance",
        product_name="falcon",
        service_type="API",
    )

    assert updated == content


def test_common_infra_paths_update_for_dev():
    component = AsporaEcsTerragruntScriptGenComponent()
    content = _load_template("api-common-alb.hcl")

    updated = component._update_common_infra_paths(content, environment="dev")

    assert 'config_path = "../common-dev-infra"' in updated
    assert '"../common-dev-infra"' in updated
