"""
Comandos de administração (Flask CLI).

    flask --app backend.wsgi promote-superadmin --user-id 1            # simulação (não altera nada)
    flask --app backend.wsgi promote-superadmin --user-id 1 --execute  # aplica

Promove um utilizador a SUPERADMIN numa única transação, com bloqueio da linha, verificação do estado e registo
de auditoria. É idempotente: se já for SUPERADMIN não altera nem regista nada. Não corre na migration nem no
arranque: só quando alguém o executa de forma explícita. Não imprime credenciais nem a connection string.
"""
import click
from sqlalchemy import select

from backend.app.models import db, User
from backend.app.services import audit


def promote_superadmin(user_id, execute=False):
    """Devolve (código, mensagem). Códigos: dry_run | promoted | already | not_found | inactive."""
    user = db.session.execute(select(User).where(User.id == user_id).with_for_update()).scalar_one_or_none()
    if user is None:
        db.session.rollback()
        return 'not_found', f'O utilizador {user_id} não existe.'
    if not user.is_active:
        db.session.rollback()
        return 'inactive', f'O utilizador {user_id} está inativo; ative-o antes de o promover.'
    if user.role == 'superadmin':
        db.session.rollback()
        return 'already', f'O utilizador {user_id} já é SUPERADMIN. Nada foi alterado.'
    if not execute:
        db.session.rollback()
        return 'dry_run', (f'Simulação: o utilizador {user_id} (papel atual: {user.role}) passaria a SUPERADMIN. '
                           'Use --execute para aplicar.')
    previous = user.role
    user.role = 'superadmin'
    audit.record(None, 'role.bootstrap_superadmin', 'success', 'user', user.id, user.username,
                 {'from': previous, 'to': 'superadmin', 'via': 'cli'})
    db.session.commit()
    return 'promoted', f'O utilizador {user_id} foi promovido a SUPERADMIN.'


def register_cli(app):
    @app.cli.command('promote-superadmin')
    @click.option('--user-id', type=int, required=True, help='ID do utilizador a promover.')
    @click.option('--execute', is_flag=True, help='Aplica a alteração (sem esta opção é só uma simulação).')
    def promote_superadmin_command(user_id, execute):
        code, message = promote_superadmin(user_id, execute)
        click.echo(message)
        if code in ('not_found', 'inactive'):
            raise SystemExit(1)
