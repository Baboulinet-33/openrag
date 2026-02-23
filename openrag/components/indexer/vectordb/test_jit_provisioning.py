import pytest
from components.indexer.vectordb.utils import (
    Base,
    Partition,
    PartitionFileManager,
    PartitionMembership,
    User,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def pfm(tmp_path):
    """Create a PartitionFileManager backed by a SQLite DB."""
    db_url = f"sqlite:///{tmp_path}/test.db"
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    pfm = object.__new__(PartitionFileManager)
    pfm.engine = engine
    pfm.Session = sessionmaker(bind=engine)
    pfm.logger = __import__("utils.logger", fromlist=["get_logger"]).get_logger()
    pfm.file_quota_per_user = -1
    return pfm


class TestGetOrCreateUserByExternalId:
    def test_creates_user_and_partition(self, pfm):
        """First call creates user, partition, and owner membership."""
        result = pfm.get_or_create_user_by_external_id("oidc-sub-123", "Alice")

        assert result["external_user_id"] == "oidc-sub-123"
        assert result["display_name"] == "Alice"
        assert result["is_admin"] is False
        assert result["token"] is None

        # Verify partition was created
        with pfm.Session() as s:
            partition = s.query(Partition).filter_by(partition="oidc-sub-123").first()
            assert partition is not None

            membership = (
                s.query(PartitionMembership).filter_by(partition_name="oidc-sub-123", user_id=result["id"]).first()
            )
            assert membership is not None
            assert membership.role == "owner"

    def test_returns_existing_user(self, pfm):
        """Second call with same external_id returns existing user."""
        first = pfm.get_or_create_user_by_external_id("oidc-sub-123", "Alice")
        second = pfm.get_or_create_user_by_external_id("oidc-sub-123", "Alice Updated")

        assert first["id"] == second["id"]
        # display_name should not change on subsequent calls
        assert second["display_name"] == "Alice"

    def test_partition_already_exists(self, pfm):
        """If partition already exists, still creates user and adds membership."""
        with pfm.Session() as s:
            s.add(Partition(partition="oidc-sub-456"))
            s.commit()

        result = pfm.get_or_create_user_by_external_id("oidc-sub-456", "Bob")

        assert result["external_user_id"] == "oidc-sub-456"
        with pfm.Session() as s:
            membership = (
                s.query(PartitionMembership).filter_by(partition_name="oidc-sub-456", user_id=result["id"]).first()
            )
            assert membership is not None
            assert membership.role == "owner"

    def test_user_has_no_opaque_token(self, pfm):
        """OIDC users should have token=NULL in the database."""
        pfm.get_or_create_user_by_external_id("oidc-sub-789", "Charlie")
        with pfm.Session() as s:
            user = s.query(User).filter_by(external_user_id="oidc-sub-789").first()
            assert user.token is None
