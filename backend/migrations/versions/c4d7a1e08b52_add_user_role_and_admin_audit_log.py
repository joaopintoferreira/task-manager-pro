"""add user role and admin audit log

Revision ID: c4d7a1e08b52
Revises: 9ee9fe5f43cf
Create Date: 2026-11-08 12:00:00.000000

Migration ADITIVA e reversível:
  * users.role VARCHAR(20) NOT NULL DEFAULT 'user' + CHECK (user | admin | superadmin)
  * admin_audit_log (histórico administrativo, sem chaves estrangeiras)

O DEFAULT 'user' mantém o código anterior a funcionar (não conhece a coluna) e dá o papel `user` a todos os
utilizadores existentes. Nenhum utilizador é promovido aqui: a promoção do primeiro SUPERADMIN é um
procedimento separado e explícito (`flask promote-superadmin`).
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c4d7a1e08b52'
down_revision = '9ee9fe5f43cf'
branch_labels = None
depends_on = None


def _is_sqlite():
    return op.get_bind().dialect.name == 'sqlite'


def upgrade():
    # NOT NULL com DEFAULT: no PostgreSQL é só metadados (sem reescrever a tabela) e os utilizadores existentes
    # ficam com 'user'. O CHECK é criado com ALTER simples; no SQLite (desenvolvimento) não existe ALTER de
    # constraints e recriar `users` falharia com FKs ativas e dados, por isso lá só se acrescenta a coluna
    # (a validação do papel continua na aplicação e, em bases criadas por create_all, no próprio modelo).
    op.add_column('users', sa.Column('role', sa.String(length=20), server_default='user', nullable=False))
    if not _is_sqlite():
        op.create_check_constraint('ck_users_role', 'users', "role IN ('user', 'admin', 'superadmin')")

    op.create_table('admin_audit_log',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('actor_id', sa.Integer(), nullable=True),
    sa.Column('actor_label', sa.String(length=120), nullable=True),
    sa.Column('action', sa.String(length=60), nullable=False),
    sa.Column('target_type', sa.String(length=30), nullable=True),
    sa.Column('target_id', sa.Integer(), nullable=True),
    sa.Column('target_label', sa.String(length=120), nullable=True),
    sa.Column('result', sa.String(length=10), nullable=False),
    sa.Column('details', sa.JSON(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_admin_audit_log_action'), 'admin_audit_log', ['action'], unique=False)
    op.create_index(op.f('ix_admin_audit_log_actor_id'), 'admin_audit_log', ['actor_id'], unique=False)
    op.create_index(op.f('ix_admin_audit_log_created_at'), 'admin_audit_log', ['created_at'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_admin_audit_log_created_at'), table_name='admin_audit_log')
    op.drop_index(op.f('ix_admin_audit_log_actor_id'), table_name='admin_audit_log')
    op.drop_index(op.f('ix_admin_audit_log_action'), table_name='admin_audit_log')
    op.drop_table('admin_audit_log')

    if not _is_sqlite():
        op.drop_constraint('ck_users_role', 'users', type_='check')
    op.drop_column('users', 'role')
