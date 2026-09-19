"""trigger_path_pattern — the on.push.paths entry each other_path becomes."""

import pytest

from app.utils.yaml_patch import clean_trigger_path, trigger_path_pattern


@pytest.mark.parametrize(
    "given, expected",
    [
        # folders
        ("shared", "shared/**"),
        ("shared/", "shared/**"),
        ("./docker/api-server", "docker/api-server/**"),
        (".github", ".github/**"),
        (".github/workflows", ".github/workflows/**"),
        # files
        (".dockerignore", ".dockerignore"),
        ("requirements.txt", "requirements.txt"),
        (".github/workflows/deploy.yml", ".github/workflows/deploy.yml"),
        ("Dockerfile", "Dockerfile"),
        ("docker/Dockerfile.kafka", "docker/Dockerfile.kafka"),
        ("Makefile", "Makefile"),
        # globs, as they are
        ("*.py", "*.py"),
        ("shared/**", "shared/**"),
        ("a/**/values/docker/**", "a/**/values/docker/**"),
        # the repo root is no filter at all
        ("", None),
        (".", None),
        ("./", None),
        (None, None),
    ],
)
def test_trigger_path_pattern(given, expected):
    assert trigger_path_pattern(given) == expected


def test_clean_trigger_path_strips_dot_slash_and_trailing_slash():
    assert clean_trigger_path("./cmd/") == "cmd"
    assert clean_trigger_path("././x") == "x"
    assert clean_trigger_path("  ") is None
