from app.infra_chat_agent.config.master_data_config import MasterData


def read_product(tenant_id: str, product: str) -> dict:
    """Get full availability details for a specific product across all regions."""
    availability = MasterData.get(tenant_id, [])
    entries = [
        {"region": e["region"], "environments": e["environments"]}
        for e in availability if e["product"] == product
    ]
    if not entries:
        return {
            "found": False,
            "message": f"Product '{product}' not found for tenant '{tenant_id}'.",
            "available_products": sorted({e["product"] for e in availability}),
        }
    return {"found": True, "product": product, "availability": entries}


def list_products(
    tenant_id: str,
    region: str | None = None,
    environment: str | None = None,
) -> dict:
    """List available products, optionally filtered by region and/or environment."""
    availability = MasterData.get(tenant_id, [])
    products = set()
    for entry in availability:
        if region and entry["region"] != region:
            continue
        if environment and environment not in entry["environments"]:
            continue
        products.add(entry["product"])
    return {"available_products": sorted(products)}
