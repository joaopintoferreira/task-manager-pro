"""
Manutenção administrativa: resumo das tabelas e limpeza global em duas etapas.

Limpeza global (só SUPERADMIN):
  1. pré-visualização: devolve as contagens e um token assinado (curta duração), ligado ao administrador e às
     contagens apresentadas;
  2. execução: exige o token, a senha, a frase escrita e `ADMIN_ALLOW_GLOBAL_WIPE=true`; as contagens são
     reverificadas dentro da transação.
Apaga só dados operacionais. Preserva contas, SUPERADMIN, esquema, alembic_version e a auditoria.
"""
import secrets

from flask import current_app
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import func, select, text

from backend.app.models import db, User, Task, Category, Notification, TaskCollaborator, RefreshToken, UserAvatar, AdminAuditLog
from backend.app.services import audit, purge
from backend.app.services.permissions import AdminError, verify_actor_password, has_permission

WIPE_PHRASE = 'LIMPAR TODOS OS DADOS'


def user_admin_fresh_actor(actor):
    """Relê o ator da base antes de apagar: tem de continuar ativo e com a permissão da limpeza global."""
    from backend.app.services.user_admin import _fresh_actor, _lock_superadmins
    _lock_superadmins()                      # serializa com as operações sobre SUPERADMIN
    fresh = _fresh_actor(actor)
    if not has_permission(fresh, 'maintenance.wipe'):
        raise AdminError('forbidden', 'Não tem permissão para esta operação.', 403)
    return fresh
WIPE_TOKEN_MAX_AGE = 300          # segundos
_SALT = 'admin-global-wipe'


def _serializer():
    return URLSafeTimedSerializer(current_app.config['SECRET_KEY'], salt=_SALT)


def wipe_enabled():
    return bool(current_app.config.get('ADMIN_ALLOW_GLOBAL_WIPE'))


def _alembic_version():
    try:
        if 'alembic_version' not in db.inspect(db.engine).get_table_names():
            return None
        return db.session.execute(text('select version_num from alembic_version')).scalar()
    except Exception:
        db.session.rollback()
        return None


def overview():
    """Contagens por tabela e informações de manutenção (só leitura)."""
    n = lambda model: db.session.execute(select(func.count()).select_from(model)).scalar_one()
    return {
        'tables': {
            'users': n(User), 'tasks': n(Task), 'categories': n(Category), 'notifications': n(Notification),
            'task_collaborators': n(TaskCollaborator), 'refresh_tokens': n(RefreshToken),
            'user_avatars': n(UserAvatar), 'admin_audit_log': n(AdminAuditLog),
        },
        'users_by_role': {r: db.session.execute(select(func.count()).select_from(User).where(User.role == r)).scalar_one()
                          for r in ('user', 'admin', 'superadmin')},
        'schema_version': _alembic_version(),
        'database': db.engine.dialect.name,
        'wipe_enabled': wipe_enabled(),
        'wipe_phrase': WIPE_PHRASE,
    }


def wipe_preview(actor):
    counts = purge.global_counts()
    token = _serializer().dumps({'a': actor.id, 'c': counts, 'n': secrets.token_hex(8)})
    audit.record(actor, 'maintenance.wipe_preview', 'success', details=counts)
    db.session.commit()
    return {'counts': counts, 'token': token, 'expires_in': WIPE_TOKEN_MAX_AGE, 'phrase': WIPE_PHRASE,
            'wipe_enabled': wipe_enabled()}


def _read_token(actor, token):
    try:
        payload = _serializer().loads(token, max_age=WIPE_TOKEN_MAX_AGE)
    except SignatureExpired:
        raise AdminError('token_expired', 'A pré-visualização expirou. Gere uma nova.', 409)
    except (BadSignature, TypeError):
        raise AdminError('token_invalid', 'Pré-visualização inválida. Gere uma nova.', 400)
    if not isinstance(payload, dict) or payload.get('a') != actor.id:
        raise AdminError('token_invalid', 'Pré-visualização inválida. Gere uma nova.', 400)
    return payload


def _token_already_used(nonce):
    """Uso único: um nonce já presente num apagamento bem-sucedido não volta a ser aceite."""
    rows = AdminAuditLog.query.filter_by(action='maintenance.wipe', result='success') \
        .order_by(AdminAuditLog.id.desc()).limit(100).all()
    return any((r.details or {}).get('nonce') == nonce for r in rows)


def wipe_execute(actor, data):
    if not wipe_enabled():
        raise AdminError('wipe_disabled', 'A limpeza global está desativada neste ambiente.', 403)
    if data.get('confirmation') != WIPE_PHRASE:
        raise AdminError('confirmation_mismatch', 'A frase de confirmação não corresponde.', 400)
    payload = _read_token(actor, data.get('token'))
    if _token_already_used(payload.get('n')):
        raise AdminError('token_used', 'Esta pré-visualização já foi utilizada. Gere uma nova.', 409)
    verify_actor_password(actor, data.get('password'))
    try:
        actor = user_admin_fresh_actor(actor)
        if db.engine.dialect.name == 'postgresql':
            db.session.execute(text("SET LOCAL statement_timeout = '25s'"))
        counts = purge.wipe_all(payload.get('c'))
        audit.record(actor, 'maintenance.wipe', 'success', details=dict(counts, nonce=payload.get('n')))
        db.session.commit()
    except purge.PurgeMismatch as exc:
        db.session.rollback()
        if exc.what == 'no_active_superadmin':
            raise AdminError('no_active_superadmin', 'Não existe nenhum SUPERADMIN ativo. Nada foi apagado.', 409)
        extra = {'current_counts': exc.found} if isinstance(exc.found, dict) else {}
        raise AdminError('counts_changed', 'Os dados mudaram desde a pré-visualização. Nada foi apagado.', 409, extra)
    return counts
