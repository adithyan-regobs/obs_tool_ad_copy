"""The view catalog must agree with the guard's denylist and the migration."""
import importlib.util
import pathlib

from app.mcp_servers.devlift_mcp.db_query import catalog


def _load_migration():
    path = pathlib.Path(__file__).resolve().parents[3] / "migrations" / "versions" / "168_add_mcp_ro_views.py"
    spec = importlib.util.spec_from_file_location("mig_168", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_catalog_view_exists_in_the_migration_and_vice_versa():
    migration = _load_migration()
    assert set(name for name, _ in migration.VIEWS) == set(catalog.VIEWS)


def test_no_catalog_column_collides_with_a_denied_word():
    # Importing executor pulls settings; derive the same denylist here instead.
    # View names may shadow a base table (search_path pins them to mcp_ro);
    # column names may not, since a bare denied word is rejected outright.
    import app.db.models  # noqa: F401
    from app.db.models.base_model import Base

    denied = (set(Base.metadata.tables) - set(catalog.VIEWS)) | {"public", "pg_catalog", "information_schema"}
    columns = {c.name for view in catalog.VIEWS.values() for c in view.columns}
    assert not (columns & denied), columns & denied


def test_describe_schema_is_json_shaped():
    doc = catalog.describe_schema()
    assert doc["schema"] == "mcp_ro"
    assert {v["name"] for v in doc["views"]} == set(catalog.VIEWS)
    assert all(v["columns"] for v in doc["views"])
    assert doc["rules"] and doc["joins"]
