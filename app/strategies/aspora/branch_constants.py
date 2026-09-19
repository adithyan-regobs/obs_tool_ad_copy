"""
Aspora Branch Constants

Defines branch protection rules and PR routing for Aspora's infrastructure.
"""

# Protected branches that require special PR handling
# PRs to these branches follow specific routing rules
PROTECTED_BRANCHES = [
    "main",
    "master",
    "pre-prod",
    "qa",
    "sandbox",
    "stage-env",
    "stage-env-copy",
    # "canada"  # Commented out for future use
]

# Branches that can NEVER be direct PR targets
# PRs to these are always redirected to pre-prod or stage-env
BLOCKED_PR_TARGETS = ["main", "master"]

# Preferred PR targets in order of preference
# System will auto-detect which exists in the repo
PR_TARGET_PREFERENCE = ["pre-prod", "stage-env"]

# Branches eligible for secondary PR (when flag enabled)
# These branches can have an optional secondary PR to themselves
# while primary PR goes to pre-prod or stage-env
SECONDARY_PR_ELIGIBLE = ["qa", "sandbox", "stage-env-copy"]

# Branches that use stage-env flow (not pre-prod flow)
# Both stage-env and stage-env-copy create PRs to stage-env
STAGE_ENV_FLOW_BRANCHES = ["stage-env", "stage-env-copy"]
