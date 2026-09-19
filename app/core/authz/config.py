from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime config. Override any field via .env or environment variables."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    fga_api_url: str = "http://localhost:8080"
    fga_store_id: str = ""
    fga_model_id: str = ""  # empty => OpenFGA uses the store's latest model

    # "demo" trusts the header below (clickable demo); "jwt" validates a
    # bearer token against the IdP settings that follow.
    auth_mode: str = "demo"
    dev_user_header: str = "x-user-id"

    # Only used when auth_mode="jwt" (requires the pyjwt[crypto] package).
    jwks_url: str = ""
    jwt_audience: str = ""
    jwt_issuer: str = ""


settings = Settings()
