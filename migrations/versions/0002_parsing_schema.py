"""Parsing schema: Paper extensions, PaperRevision, PaperSection, Reference."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("papers") as batch_op:
        batch_op.add_column(sa.Column("title", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("authors", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("abstract", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("primary_category", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("categories", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("published_at", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("published_date_precision", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("published_date_source", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("created_at", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("updated_at", sa.Float(), nullable=True))

    op.create_table(
        "paper_revisions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "paper_id", sa.String(), sa.ForeignKey("papers.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("arxiv_version", sa.String(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=True),
        sa.Column("pdf_source_url", sa.String(), nullable=True),
        sa.Column("parser_version", sa.String(), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("paper_id", "arxiv_version", name="uq_paper_revision"),
    )
    op.create_index("ix_paper_revisions_paper_id", "paper_revisions", ["paper_id"])

    op.create_table(
        "paper_sections",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "revision_id",
            sa.String(),
            sa.ForeignKey("paper_revisions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "paper_id", sa.String(), sa.ForeignKey("papers.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("section_index", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("text", sa.String(), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=True),
        sa.Column("page_end", sa.Integer(), nullable=True),
        sa.Column("span_info", sa.JSON(), nullable=True),
        sa.Column("extraction_quality", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("revision_id", "section_index", name="uq_revision_section_index"),
    )
    op.create_index("ix_paper_sections_revision_id", "paper_sections", ["revision_id"])
    op.create_index("ix_paper_sections_paper_id", "paper_sections", ["paper_id"])

    op.create_table(
        "paper_references",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "revision_id",
            sa.String(),
            sa.ForeignKey("paper_revisions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "paper_id", sa.String(), sa.ForeignKey("papers.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("ref_order", sa.Integer(), nullable=False),
        sa.Column("raw_text", sa.String(), nullable=False),
        sa.Column("citation_key", sa.String(), nullable=True),
        sa.Column(
            "matched_paper_id",
            sa.String(),
            sa.ForeignKey("papers.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("matched_arxiv_id", sa.String(), nullable=True),
        sa.Column("matched_doi", sa.String(), nullable=True),
        sa.Column("resolution_status", sa.String(), nullable=False, server_default="unresolved"),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("revision_id", "ref_order", name="uq_revision_ref_order"),
    )
    op.create_index("ix_paper_references_revision_id", "paper_references", ["revision_id"])
    op.create_index("ix_paper_references_paper_id", "paper_references", ["paper_id"])
    op.create_index(
        "ix_paper_references_matched_paper_id", "paper_references", ["matched_paper_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_paper_references_matched_paper_id", table_name="paper_references")
    op.drop_index("ix_paper_references_paper_id", table_name="paper_references")
    op.drop_index("ix_paper_references_revision_id", table_name="paper_references")
    op.drop_table("paper_references")

    op.drop_index("ix_paper_sections_paper_id", table_name="paper_sections")
    op.drop_index("ix_paper_sections_revision_id", table_name="paper_sections")
    op.drop_table("paper_sections")

    op.drop_index("ix_paper_revisions_paper_id", table_name="paper_revisions")
    op.drop_table("paper_revisions")

    with op.batch_alter_table("papers") as batch_op:
        batch_op.drop_column("updated_at")
        batch_op.drop_column("created_at")
        batch_op.drop_column("published_date_source")
        batch_op.drop_column("published_date_precision")
        batch_op.drop_column("published_at")
        batch_op.drop_column("categories")
        batch_op.drop_column("primary_category")
        batch_op.drop_column("abstract")
        batch_op.drop_column("authors")
        batch_op.drop_column("title")
