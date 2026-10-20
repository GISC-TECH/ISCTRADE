from sqlalchemy.orm import configure_mappers

from app.models import Account, Base, User, Workspace


def test_mappers_configure() -> None:
    configure_mappers()
    assert {"users", "workspaces", "workspace_members", "accounts"} <= set(Base.metadata.tables)
    assert Workspace.accounts.property.mapper.class_ is Account
    assert "encrypted_api_key" in Account.__table__.c
    assert User.__table__.c.email.unique
