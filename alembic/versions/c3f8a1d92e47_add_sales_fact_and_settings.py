"""add sales_fact / app_setting / topic.published_video_external_id

Revision ID: c3f8a1d92e47
Revises: b88d2b2ed14e
Create Date: 2026-10-11 20:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3f8a1d92e47'
down_revision: Union[str, Sequence[str], None] = 'b88d2b2ed14e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'sales_fact',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('video_external_id', sa.String(length=64), nullable=False),
        sa.Column('product_external_id', sa.String(length=64), nullable=False),
        sa.Column('stat_date', sa.String(length=20), nullable=False),
        sa.Column('sales_count', sa.Integer(), nullable=False),
        sa.Column('gmv', sa.Float(), nullable=False),
        sa.Column('return_count', sa.Integer(), nullable=False),
        sa.Column('return_amount', sa.Float(), nullable=False),
        sa.Column('imported_batch', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'app_setting',
        sa.Column('key', sa.String(length=64), nullable=False),
        sa.Column('value', sa.Text(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('key'),
    )
    op.add_column(
        'topic',
        sa.Column('published_video_external_id', sa.String(length=64), nullable=False, server_default=''),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('topic', 'published_video_external_id')
    op.drop_table('app_setting')
    op.drop_table('sales_fact')
