from app.infra_chat_agent.config.master_data_config import MasterData


def read_service(tenant_id: str, service: str) -> dict:
    """Get all region+product+environment combinations where a service is deployed."""
    availability = MasterData.get(tenant_id, [])
    entries = []
    for e in availability:
        for env, services in e["environments"].items():
            if service in services:
                entries.append({"region": e["region"], "product": e["product"], "environment": env})
    if not entries:
        all_services = set()
        for e in availability:
            for svc_list in e["environments"].values():
                all_services.update(svc_list)
        return {
            "found": False,
            "message": f"Service '{service}' not found for tenant '{tenant_id}'.",
            "available_services": sorted(all_services),
        }
    return {"found": True, "service": service, "available_in": entries}


def list_services(
    tenant_id: str,
    region: str | None = None,
    product: str | None = None,
    environment: str | None = None,
) -> dict:
    """List available services, optionally filtered by region, product, and/or environment."""
    availability = MasterData.get(tenant_id, [])
    services = set()
    for entry in availability:
        if region and entry["region"] != region:
            continue
        if product and entry["product"] != product:
            continue
        if environment:
            if environment in entry["environments"]:
                services.update(entry["environments"][environment])
        else:
            for svc_list in entry["environments"].values():
                services.update(svc_list)
    return {"available_services": sorted(services)}


