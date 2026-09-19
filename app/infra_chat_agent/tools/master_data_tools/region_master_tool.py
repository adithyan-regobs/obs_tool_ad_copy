from app.infra_chat_agent.config.master_data_config import MasterData


def read_region(tenant_id: str, region: str) -> dict:
    """Get full availability details for a specific region across all products."""
    availability = MasterData.get(tenant_id, [])
    entries = [
        {"product": e["product"], "environments": e["environments"]}
        for e in availability if e["region"] == region
    ]
    if not entries:
        return {
            "found": False,
            "message": f"Region '{region}' not found for tenant '{tenant_id}'.",
            "available_regions": sorted({e["region"] for e in availability}),
        }
    return {"found": True, "region": region, "availability": entries}


def list_regions(
    tenant_id: str,
    product: str | None = None,
    environment: str | None = None,
) -> dict:
    """List available regions, optionally filtered by product and/or environment."""
    availability = MasterData.get(tenant_id, [])
    regions = set()
    for entry in availability:
        if product and entry["product"] != product:
            continue
        if environment and environment not in entry["environments"]:
            continue
        regions.add(entry["region"])
    return {"available_regions": sorted(regions)}
