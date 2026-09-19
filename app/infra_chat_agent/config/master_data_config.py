
MasterData: dict[str, list[dict]] = {
    "vance": [
        {"region": "london", "product": "core", "environments": {
            "prod": ["appserver-service","goms-service","workflow-service","fx-api-service","fx-service","beneficiary-service","verification-service","eventbus-service","reward-service","lulu-fulfillment-service","goblin-service","recko-service","user-vault-service","bbps-service"],
        }},
        {"region": "london", "product": "falcon", "environments": {

        }},
        {"region": "canada", "product": "core", "environments": {
            "prod": ["appserver-service","goms-service","workflow-service","fx-api-service","fx-service","beneficiary-service","verification-service","eventbus-service","reward-service","lulu-fulfillment-service","goblin-service","recko-service","user-vault-service","bbps-service"],

        }},
        {"region": "canada", "product": "falcon", "environments": {

        }},
        {"region": "mumbai", "product": "core", "environments": {
            "stage": ["appserver-service", "reward-api-service","goms-service","lulu-fulfillment-service","workflow-service","fx-api-service","fx-service","beneficiary-service","verification-service","eventbus-service","user-vault-service","bbps-service","pulse-service","goblin-service","payment-platform","canvars.ai"],
            "qa":    ["reward-api-service", "goms-service","lulu-fulfillment-service","workflow-service","fx-service,beneficiary-service"],
            "prod":  ["ybl-fulfillment-service", "rda-service"],
        }},
        {"region": "mumbai", "product": "falcon", "environments": {

        }},
    ],
    "aspora": [
         {"region": "london", "product": "core", "environments": {
           "dev" : ["user-vault"],
           "prod" : ["alphadesk-api", "app-server", "app-server-internal", "backoffice", "bbps", "bbps-worker", "beneficiary", "beneficiary", "canvas", "cron", "devops-sandbox", "email", "eventbus", "fx", "fx-api", "fx_worker", "genie-service", "goblin", "goms", "lulu-fulfillment", "lulu-fulfillment", "notification", "ponzim", "recko", "recon", "reminder", "reminder-worker", "rewards-api", "rewards-api-internal", "rewards-worker", "settlement-api-service", "settlements-api", "settlements-worker", "user-vault", "verification", "workflow", "workflow", "workflow-op", "ybl-fulfillment"]
        }},
        {"region": "canada", "product": "core", "environments": {
          "prod" : ["alphadesk-api", "app-server", "app-server-internal", "backoffice", "bbps", "bbps-worker", "beneficiary", "cron", "devlift-test", "DevLift Test", "devlift_test_api", "devops-sandbox-golang", "email", "eventbus", "fx", "goblin", "goms", "lulu-fulfillment", "notification", "payment-platform", "ponzim", "rewards-api", "rewards-worker", "rewards-worker", "settlements-api", "settlements-worker", "test", "test_demo", "test-ops-tool", "test_worker", "user-vault", "verification", "workflow"],
        }},
        {"region": "mumbai", "product": "core", "environments": {
            "dev" : ["alphadesk-api", "app-server", "app-server-internal", "backoffice", "bbps", "bbps-worker", "beneficiary", "casa", "cron", "DevLift Test", "devops-sandbox", "email", "eventbus", "goblin", "goms", "kabutar", "lucifer", "lulu-fulfillment", "notification", "ponzim", "pulse-backend", "qbee", "recko", "recon", "rewards-api", "rewards-worker", "settlements-api", "settlements-worker", "user-vault", "verification", "workflow", "ybl-fulfillment"],
            "stage" : ["accounting-service", "accounting-transformation-service", "accounting-transform-service", "ai-bot", "alphadesk-api", "app-server", "app-server-internal", "aspora-people", "backoffice", "bbps", "bbps-worker", "bbps-worker", "beneficiary", "beneficiary-service", "canvas", "casa", "cron", "cron", "devlift-test", "devlift_test_api", "devops-sandbox", "devops-sandbox-golang", "email", "eventbus", "flowcraft-service", "flowcraft-ui", "fx-api", "fx_worker", "genie-service", "goblin", "goms", "india_pulse", "kabutar", "Koda WhatsApp Bot", "Koda WhatsApp Bot", "lucifer", "lulu-fulfillment", "minipit", "notification", "notification", "package-service", "payment-platform", "ponzim", "pulse-backend", "qbee", "recko", "recon", "reminder", "reminder-worker", "rewards-api", "rewards-worker", "settlements-api", "settlements-worker", "settlements-worker", "test", "testcraft-service", "testcraft-service", "testcraft-ui", "testcraft-ui", "test_demo", "test-ops-tool", "test_worker", "test_worker", "user-vault", "verification", "wealth", "workflow", "ybl-fulfillment"],
            "qa" : ["beneficiary", "beneficiary-service", "devlift-test", "goblin", "rewards-api", "rewards-worker", "test_ops", "test-ops-tool", "test-ops-tool"],
            "prod" : ["devlift-test", "devlift_test_api", "devops-sandbox", "package-service", "ybl-fulfillment"]
        }},
        {"region": "mumbai", "product": "falcon", "environments": {
            "stage" : ["falcon-api", "falcon-consumer", "falcon-worker"]
        }},
    ],
}







ServiceMasterData: dict[str, dict] = {
    # --- Core backend services ---
    "app-server":              {"code": "d90c2a75-9142-41ac-a4ce-f0075733ad83", "name": "App Server"},
    "app-server-internal":     {"code": "b11db949-8a62-488b-86bd-d048fdf088d1", "name": "App Server Internal"},
    "backoffice":              {"code": "0252b039-6eec-49bf-a924-790e1e155bfe", "name": "Backoffice"},
    "casa":                    {"code": "671bcbf4-90b4-498d-b69f-ad5370af1087", "name": "Casa"},
    "cron":                    {"code": "6fdc78bb-9a6d-48ae-9c4e-fac630cb1ffd", "name": "Cron"},
    "email":                   {"code": "24f82528-39c7-4f97-9634-e9e893442a80", "name": "Email"},
    "eventbus":                {"code": "87ce3086-fb5a-4ade-8b59-624be4794b51", "name": "Eventbus"},
    "goms":                    {"code": "ac5b3bb7-22ba-4ab9-b1ad-3b577e7f97e5", "name": "GOMS"},
    "notification":            {"code": "9c43e5cd-6489-4206-8a9d-3253bd9ca833", "name": "Notification"},
    "workflow":                {"code": "f7513b9e-c6ab-4a8e-a61b-b1afeb604829", "name": "Workflow"},

    # --- Financial / payment services ---
    "beneficiary":             {"code": "cb244962-6585-45c4-9fda-dd04189f5b0f", "name": "Beneficiary"},
    "beneficiary-service":     {"code": "51201e49-caeb-4562-81ec-8bf701443b0d", "name": "Beneficiary Service"},
    "bbps":                    {"code": "4dbbac4f-e4ff-4cf2-9705-4a7e8dce2b3a", "name": "BBPS"},
    "bbps-worker":             {"code": "9dca29b5-f16c-4751-9d3e-28e886985bb3", "name": "BBPS Worker"},
    "fx":                      {"code": "e029d221-25bd-4f46-808b-a0f2c1d48c55", "name": "FX"},
    "fx-api":                  {"code": "77a51f13-849c-483e-8b9a-b4778eeb5158", "name": "FX API"},
    "fx_worker":               {"code": "607ee48c-bd81-4ad0-ab5e-5c51bf7bf369", "name": "FX Worker"},
    "lulu-fulfillment":        {"code": "356c2d96-dc54-44f3-beb7-b77037e95b21", "name": "Lulu Fulfillment"},
    "payment-platform":        {"code": "6b94e2ef-e2bb-4a2e-917e-e6aff7b7e563", "name": "Payment Platform"},
    "recon":                   {"code": "677845cb-5e57-4d0a-bc43-e9f041bcce6b", "name": "Recon"},
    "recko":                   {"code": "463aa85c-e5be-473a-8dad-a3a9f6e1651a", "name": "Recko"},
    "settlements-api":         {"code": "78436652-d273-4644-8407-de008567bd3f", "name": "Settlements API"},
    "settlements-worker":      {"code": "b1f079df-c810-485b-8bb7-86005b097cfb", "name": "Settlements Worker"},
    "ybl-fulfillment":         {"code": "d4b39b60-c55d-4f45-a3cc-6b2237d29b84", "name": "YBL Fulfillment"},

    # --- Rewards / loyalty ---
    "rewards-api":             {"code": "0c16857d-f9ea-4e27-b79f-b98ef6be216e", "name": "Rewards API"},
    "rewards-worker":          {"code": "0a05b95e-2255-4931-bd5b-75df0f2b6c0b", "name": "Rewards Worker"},

    # --- Analytics / other ---
    "wealth":                  {"code": "97e089de-31f8-4221-a8f0-d3a31571325a", "name": "Wealth"},

    # --- Platform / infra services ---
    "goblin":                  {"code": "caaa926b-c91a-4a93-a4a4-493c3aec607f", "name": "Goblin"},
    "kabutar":                 {"code": "0b69d044-785d-4974-b1d7-75e293aa2f07", "name": "Kabutar"},
    "lucifer":                 {"code": "1907a0ef-0d5f-4a22-a642-f65cf7d52c2a", "name": "Lucifer"},
    "ponzim":                  {"code": "b9155a5f-5df5-4cc0-938b-06acc8d060b9", "name": "Ponzim"},
    "pulse-backend":           {"code": "f531d014-4ef5-4390-925e-961912061842", "name": "Pulse Backend"},
    "qbee":                    {"code": "f8b1fe5b-e3a2-49cd-a727-d32b2c255e10", "name": "Qbee"},
    "rhythm":                  {"code": "1cf0e862-6ab4-45d3-b7c7-57c01c5268d7", "name": "Rhythm"},
    "user-vault":              {"code": "90606d1e-9ac1-4fb7-96a1-d651581f4733", "name": "User Vault"},
    "verification":            {"code": "40b931f1-2251-4582-8307-9464cf469e51", "name": "Verification"},

    # --- Falcon services ---
    "falcon-api":              {"code": "474a9e21-3e62-439d-ac41-54c3c0a8c62b", "name": "Falcon API"},
    "falcon-consumer":         {"code": "06f876e4-c0d8-48d3-9d29-9c3fd464761f", "name": "Falcon Consumer"},
    "falcon-worker":           {"code": "53757661-a66b-47cc-bcb7-33e5fed67c4e", "name": "Falcon Worker"},

    # --- Analytics / AI ---
    "ai-bot":                  {"code": "6fca4932-3750-4497-a088-85fc81fdfa12", "name": "AI Bot"},
    "alphadesk-api":           {"code": "78355504-5d29-4f4c-b8b3-d2506b581812", "name": "Alphadesk API"},
    "canvas":                  {"code": "6c023456-bc6b-4334-83a7-27a325969d74", "name": "Canvas"},

    # --- Reminder ---
    "reminder":                {"code": "7b4d2b7e-492e-4066-8d9e-e4fcfb72aed4", "name": "Reminder"},
    "reminder-worker":         {"code": "d16ddf6b-d2d6-46f8-aff4-2923bc92faa4", "name": "Reminder Worker"},

    # --- DevOps / test services ---
    "devops-sandbox":          {"code": "35ca15cc-5337-4fad-b9b9-318530db1e79", "name": "DevOps Sandbox"},
    "devops-sandbox-golang":   {"code": "a4db0a36-5b36-4c2d-8070-5bbd32b1d77e", "name": "DevOps Sandbox Golang"},
    "DevLift Test":            {"code": "85c9007d-860a-4b06-96cd-ec8a7f953279", "name": "DevLift Test"},
    "devlift-test":            {"code": "13b1696e-cdbc-45f6-89fa-c60952062588", "name": "DevLift Test"},
    "devlift_test_api":        {"code": "b26f7f36-9d96-449b-b404-8fa59b9c38ab", "name": "DevLift Test API"},
    "hartest":                 {"code": "0690c2e7-cbc2-4669-980e-ff300bd1a5a0", "name": "Hartest"},
    "obstooltest":             {"code": "d60f15bd-a6b3-469b-9eff-86cd1a66addc", "name": "ObsTool Test"},
    "auto_scaling_test_1":     {"code": "3870fd13-428e-4d28-86a4-8424b759481e", "name": "Auto Scaling Test 1"},
    "auto_scaling_test-2":     {"code": "fe418e50-886d-4a38-ac29-7231305bd050", "name": "Auto Scaling Test 2"},
    "test":                    {"code": "a4d91f72-9346-4ba3-8759-fcd69fce1691", "name": "Test"},
    "test 2":                  {"code": "014ae254-a54d-4af4-b288-b88b6b498efd", "name": "Test 2"},
    "test_demo":               {"code": "6bef9493-88c6-48e7-ad1b-f1bc9f7e73bf", "name": "Test Demo"},
    "test-ops-tool":           {"code": "a7e9f71b-1204-4c91-b5dd-8c2771e03cd4", "name": "Test Ops Tool"},
    "test_ops":                {"code": "4f63660a-1425-436c-9a8b-400049bc4b9e", "name": "Test Ops"},
    "test_worker":             {"code": "5fcb7f8e-8f0f-4484-82b6-52f7363a180c", "name": "Test Worker"},
}



def get_services_for_tenant(tenant_id: str) -> list[str]:
    """Get all unique services across all entries for a tenant."""
    availability = MasterData.get(tenant_id, [])
    seen: set[str] = set()
    result: list[str] = []
    for entry in availability:
        for svc_list in entry["environments"].values():
            for svc in svc_list:
                if svc not in seen:
                    seen.add(svc)
                    result.append(svc)
    return result


def get_services_for_entry(
    tenant_id: str,
    region: str | None = None,
    product: str | None = None,
    environment: str | None = None,
) -> list[str]:
    """Get services narrowed by region/product/environment. Falls back to all if no match."""
    availability = MasterData.get(tenant_id, [])
    entries = availability
    if region:
        entries = [e for e in entries if e["region"] == region]
    if product:
        entries = [e for e in entries if e["product"] == product]
    if not entries:
        entries = availability
    seen: set[str] = set()
    result: list[str] = []
    for entry in entries:
        if environment and environment in entry["environments"]:
            for svc in entry["environments"][environment]:
                if svc not in seen:
                    seen.add(svc)
                    result.append(svc)
        else:
            for svc_list in entry["environments"].values():
                for svc in svc_list:
                    if svc not in seen:
                        seen.add(svc)
                        result.append(svc)
    return result




def build_master_data_summary(tenant_id: str) -> str:
    """Build a human-readable summary of all master data for a tenant.

    Sample output for vance:
    -------------------------------------------------------
    MasterData Availability Matrix (region -> product -> environment -> services):
    Regions are geolocations. Each region hosts products.
    Each product has environments (deployment stages: dev -> qa -> stage -> prod).
    Each environment has specific services deployed to it.
    Not all services are available in every environment.

      london:
        core:
          dev: payment-service, user-service, auth-service
          qa: payment-service, auth-service
          prod: payment-service, user-service, auth-service
        falcon:
          dev: order-service, notification-service
          prod: order-service
    -------------------------------------------------------
    """
    availability = MasterData.get(tenant_id, [])
    if not availability:
        return ""

    by_region: dict[str, list[dict]] = {}
    for entry in availability:
        by_region.setdefault(entry["region"], []).append(entry)

    lines = [
        "MasterData Availability Matrix (region -> product -> environment -> services):",
        "Regions are geolocations. Each region hosts products.",
        "Each product has environments (deployment stages: dev -> qa -> stage -> prod).",
        "Each environment has specific services deployed to it.",
        "Not all services are available in every environment.",
        "",
    ]
    for region, entries in by_region.items():
        lines.append(f"  {region}:")
        for entry in entries:
            lines.append(f"    {entry['product']}:")
            for env, services in entry["environments"].items():
                svcs = ", ".join(services) if services else "(no services)"
                lines.append(f"      {env}: {svcs}")

    return "\n".join(lines)


