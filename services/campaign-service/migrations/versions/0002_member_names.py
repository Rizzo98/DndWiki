"""campaign_members: surrogate id PK, member names, optional user link

Members become players at the table (player_name/character_name) with an
optional link to a platform user (user_id now nullable). The old composite
PK (campaign_id, user_id) moves to a surrogate id PK plus a unique
(campaign_id, user_id) constraint (NULLs exempt, so many unlinked members
can coexist).

Revision ID: 0002
Revises: 0001
Create Date: 2025-01-02

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def _upgrade_postgres() -> None:
    op.add_column("campaign_members", sa.Column("id", sa.Uuid(), nullable=True))
    op.add_column(
        "campaign_members",
        sa.Column("player_name", sa.String(length=255), nullable=False, server_default=""),
    )
    op.add_column(
        "campaign_members",
        sa.Column("character_name", sa.String(length=255), nullable=False, server_default=""),
    )
    op.execute("UPDATE campaign_members SET id = gen_random_uuid() WHERE id IS NULL")
    # Existing rows get presentable defaults: the DM row is "Dungeon Master",
    # players are named "Player" (the DM can rename both via the UI).
    op.execute(
        "UPDATE campaign_members SET player_name = 'Dungeon Master', character_name = 'Dungeon Master' WHERE role = 'dm'"
    )
    op.execute("UPDATE campaign_members SET player_name = 'Player' WHERE role = 'player'")
    op.alter_column("campaign_members", "id", existing_type=sa.Uuid(), nullable=False)
    op.drop_constraint("campaign_members_pkey", "campaign_members", type_="primary")
    op.create_primary_key("campaign_members_pkey", "campaign_members", ["id"])
    op.alter_column("campaign_members", "user_id", existing_type=sa.Uuid(), nullable=True)
    op.create_unique_constraint(
        "uq_campaign_members_campaign_user", "campaign_members", ["campaign_id", "user_id"]
    )
    op.create_index("ix_campaign_members_campaign_id", "campaign_members", ["campaign_id"])


def _upgrade_sqlite() -> None:
    """SQLite cannot alter PKs/constraints in place; recreate the table explicitly.

    The pre-0002 table is recreated as campaign_members_new with the new
    schema, rows are copied (id backfilled, names defaulted), and the table is
    swapped in. This avoids depending on reflected constraint names, which
    SQLite does not preserve for inline PKs.
    """
    op.execute(
        """
        CREATE TABLE campaign_members_new (
            id CHAR(32) NOT NULL,
            campaign_id CHAR(32) NOT NULL,
            user_id CHAR(32),
            role VARCHAR(16) NOT NULL DEFAULT 'player',
            player_name VARCHAR(255) NOT NULL DEFAULT '',
            character_name VARCHAR(255) NOT NULL DEFAULT '',
            joined_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT uq_campaign_members_campaign_user UNIQUE (campaign_id, user_id),
            CONSTRAINT fk_campaign_members_campaign_id
                FOREIGN KEY (campaign_id) REFERENCES campaigns (id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        """
        INSERT INTO campaign_members_new
            (id, campaign_id, user_id, role, player_name, character_name, joined_at)
        SELECT lower(hex(randomblob(16))), campaign_id, user_id, role,
               CASE WHEN role = 'dm' THEN 'Dungeon Master' ELSE 'Player' END,
               CASE WHEN role = 'dm' THEN 'Dungeon Master' ELSE '' END,
               joined_at
        FROM campaign_members
        """
    )
    op.execute("DROP TABLE campaign_members")
    op.execute("ALTER TABLE campaign_members_new RENAME TO campaign_members")
    op.execute("CREATE INDEX ix_campaign_members_user_id ON campaign_members (user_id)")
    op.execute("CREATE INDEX ix_campaign_members_campaign_id ON campaign_members (campaign_id)")


def upgrade() -> None:
    if _is_sqlite():
        _upgrade_sqlite()
    else:
        _upgrade_postgres()


def _downgrade_postgres() -> None:
    # Unlinked members cannot be represented by the old schema; drop them.
    op.execute("DELETE FROM campaign_members WHERE user_id IS NULL")
    op.drop_index("ix_campaign_members_campaign_id", table_name="campaign_members")
    op.drop_constraint("uq_campaign_members_campaign_user", "campaign_members", type_="unique")
    op.alter_column("campaign_members", "user_id", existing_type=sa.Uuid(), nullable=False)
    op.drop_constraint("campaign_members_pkey", "campaign_members", type_="primary")
    op.create_primary_key("campaign_members_pkey", "campaign_members", ["campaign_id", "user_id"])
    op.drop_column("campaign_members", "character_name")
    op.drop_column("campaign_members", "player_name")
    op.drop_column("campaign_members", "id")


def _downgrade_sqlite() -> None:
    # Unlinked members cannot be represented by the old schema; drop them.
    op.execute("DELETE FROM campaign_members WHERE user_id IS NULL")
    op.execute("DROP INDEX ix_campaign_members_campaign_id")
    op.execute("DROP INDEX ix_campaign_members_user_id")
    op.execute(
        """
        CREATE TABLE campaign_members_old (
            campaign_id CHAR(32) NOT NULL,
            user_id CHAR(32) NOT NULL,
            role VARCHAR(16) NOT NULL DEFAULT 'player',
            joined_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
            PRIMARY KEY (campaign_id, user_id)
        )
        """
    )
    op.execute(
        """
        INSERT INTO campaign_members_old (campaign_id, user_id, role, joined_at)
        SELECT campaign_id, user_id, role, joined_at FROM campaign_members
        """
    )
    op.execute("DROP TABLE campaign_members")
    op.execute("ALTER TABLE campaign_members_old RENAME TO campaign_members")
    op.execute("CREATE INDEX ix_campaign_members_user_id ON campaign_members (user_id)")


def downgrade() -> None:
    if _is_sqlite():
        _downgrade_sqlite()
    else:
        _downgrade_postgres()
