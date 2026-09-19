from app.infra_chat_agent.config.master_data_config import MasterData


def read_environment(tenant_id: str, environment: str) -> dict:
    """Get all product+region combinations where a specific environment is available."""
    availability = MasterData.get(tenant_id, [])
    entries = [
        {"product": e["product"], "region": e["region"]}
        for e in availability if environment in e["environments"]
    ]
    if not entries:
        all_envs = set()
        for e in availability:
            all_envs.update(e["environments"])
        return {
            "found": False,
            "message": f"Environment '{environment}' not found for tenant '{tenant_id}'.",
            "available_environments": sorted(all_envs),
        }
    return {"found": True, "environment": environment, "available_in": entries}


def list_environments(
    tenant_id: str,
    product: str | None = None,
    region: str | None = None,
) -> dict:
    """List available environments, optionally filtered by product and/or region."""
    availability = MasterData.get(tenant_id, [])
    environments = set()
    for entry in availability:
        if product and entry["product"] != product:
            continue
        if region and entry["region"] != region:
            continue
        for env in entry["environments"]:
            environments.add(env)
    return {"available_environments": sorted(environments)}
