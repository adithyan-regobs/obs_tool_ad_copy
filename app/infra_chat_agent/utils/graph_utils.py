def get_tenant_id(state: dict, default:str = "aspora") -> str:
    return state.get("tenant_id", default)
