"""
Operações administrativas sobre utilizadores, com as invariantes de segurança:

  * ninguém altera o PRÓPRIO papel, estado ou conta através do painel;
  * um ADMIN nunca age sobre ADMIN/SUPERADMIN (as permissões dependem do papel do ALVO);
  * o último SUPERADMIN ativo nunca é desativado, despromovido nem excluído — a verificação ocorre na mesma
    transação, com as linhas de SUPERADMIN bloqueadas (`SELECT ... FOR UPDATE`, sempre pela mesma ordem de id);
  * as operações destrutivas exigem a senha do administrador, a confirmação escrita do alvo e as contagens que
    ele viu; tudo é reverificado dentro da transação.

No PostgreSQL o bloqueio de linhas evita corridas entre dois administradores. O SQLite dos testes ignora
`FOR UPDATE`: a lógica é testada, mas a concorrência real só se valida num PostgreSQL.
"""
from sqlalchemy import select

from backend.app.models import db, User, UserAvatar
from backend.app.services import audit, purge
from backend.app.services.permissions import (
    AdminError, ROLE_USER, ROLE_ADMIN, ROLE_SUPERADMIN, ROLES,
    TOGGLE_ACTIVE_PERMISSION, REMOVE_AVATAR_PERMISSION, has_permission, verify_actor_password,
)

# Transições de papel permitidas (as outras exigem passar por ADMIN)
ROLE_TRANSITIONS = {
    (ROLE_USER, ROLE_ADMIN), (ROLE_ADMIN, ROLE_USER),
    (ROLE_ADMIN, ROLE_SUPERADMIN), (ROLE_SUPERADMIN, ROLE_ADMIN),
}


# ──────────────────────────────────────────────
# Utilitários
# ──────────────────────────────────────────────
def admin_user_dict(user):
    """Campos mínimos do utilizador para o painel (nunca hash, tokens nem dados internos)."""
    d = user.to_dict()
    return {
        'id': d['id'], 'username': d['username'], 'email': d['email'], 'role': user.role,
        'is_active': bool(user.is_active), 'has_avatar': d['has_avatar'],
        'created_at': d['created_at'], 'last_login': _iso(user.last_login),
    }


def _iso(dt):
    from backend.app.models import iso_utc
    return iso_utc(dt)


def allowed_actions(actor, target):
    """O que o ator pode fazer sobre o alvo (serve para a interface; o backend volta a verificar tudo)."""
    own = actor.id == target.id
    return {
        'toggle_active': (not own) and has_permission(actor, TOGGLE_ACTIVE_PERMISSION.get(target.role, '-')),
        'remove_avatar': (not own) and bool(target.to_dict()['has_avatar'])
                         and has_permission(actor, REMOVE_AVATAR_PERMISSION.get(target.role, '-')),
        'clear_data':    has_permission(actor, 'users.clear_data'),
        'delete':        (not own) and has_permission(actor, 'users.delete'),
        'change_role':   (not own) and has_permission(actor, 'roles.manage'),
    }


def _lock_superadmins():
    # populate_existing: depois de esperar por um bloqueio, as linhas têm de ser relidas (o estado pode ter mudado)
    return db.session.execute(
        select(User).where(User.role == ROLE_SUPERADMIN).order_by(User.id).with_for_update()
        .execution_options(populate_existing=True)
    ).scalars().all()


def _lock_target(target_id):
    """Bloqueia os SUPERADMIN (ordem por id) e depois o alvo. Devolve (alvo, superadmins)."""
    superadmins = _lock_superadmins()
    target = next((u for u in superadmins if u.id == target_id), None)
    if target is None:
        target = db.session.execute(
            select(User).where(User.id == target_id).with_for_update().execution_options(populate_existing=True)
        ).scalar_one_or_none()
    if target is None:
        raise AdminError('not_found', 'Utilizador não encontrado.', 404)
    return target, superadmins


def _fresh_actor(actor):
    """Relê o ator da base (já com os bloqueios feitos): quem foi desativado ou despromovido entretanto não age."""
    fresh = db.session.execute(
        select(User).where(User.id == actor.id).execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if fresh is None or not fresh.is_active:
        raise AdminError('inactive_actor', 'A sua conta foi desativada.', 401)
    return fresh


def _begin(actor, base_permission, target_id=None):
    """Início de uma operação: bloqueia linhas, relê o ator e confirma a permissão base."""
    if target_id is None:
        target, superadmins = None, _lock_superadmins()
    else:
        target, superadmins = _lock_target(target_id)
    actor = _fresh_actor(actor)
    _need(actor, base_permission)
    return actor, target, superadmins


def _is_last_active_superadmin(target, superadmins):
    return (target.role == ROLE_SUPERADMIN and bool(target.is_active)
            and sum(1 for u in superadmins if u.is_active) <= 1)


def _no_self(actor, target):
    if actor.id == target.id:
        raise AdminError('self_action', 'Esta operação não pode ser aplicada à sua própria conta pelo painel.', 403)


def _need(actor, permission):
    if not has_permission(actor, permission):
        raise AdminError('forbidden', 'Não tem permissão para esta operação.', 403)


def _confirm_destructive(actor, target, data):
    confirm = data.get('confirm_username')
    if not isinstance(confirm, str) or confirm.strip() != target.username:
        raise AdminError('confirmation_mismatch', 'A confirmação escrita não corresponde ao nome do utilizador.', 400)
    verify_actor_password(actor, data.get('password'))


def _label(user):
    return user.username


# ──────────────────────────────────────────────
# Operações simples
# ──────────────────────────────────────────────
def set_active(actor, target_id, active):
    actor, target, superadmins = _begin(actor, 'users.list', target_id)
    _no_self(actor, target)
    _need(actor, TOGGLE_ACTIVE_PERMISSION.get(target.role, '-'))
    if bool(target.is_active) == active:
        raise AdminError('no_change', 'A conta já está neste estado.', 409)
    if not active and _is_last_active_superadmin(target, superadmins):
        raise AdminError('last_superadmin', 'Não é possível desativar o último SUPERADMIN ativo.', 409)
    target.is_active = active
    audit.record(actor, 'user.activate' if active else 'user.deactivate', 'success', 'user', target.id, _label(target),
                 {'role': target.role})
    db.session.commit()
    return target


def remove_avatar(actor, target_id):
    actor, target, _ = _begin(actor, 'users.list', target_id)
    _no_self(actor, target)
    _need(actor, REMOVE_AVATAR_PERMISSION.get(target.role, '-'))
    avatar = db.session.get(UserAvatar, target.id)
    if avatar is None:
        raise AdminError('no_avatar', 'O utilizador não tem foto de perfil.', 404)
    db.session.delete(avatar)
    audit.record(actor, 'user.avatar_removed', 'success', 'user', target.id, _label(target), {'role': target.role})
    db.session.commit()
    return target


# ──────────────────────────────────────────────
# Papéis
# ──────────────────────────────────────────────
def set_role(actor, target_id, new_role, data):
    if new_role not in ROLES:
        raise AdminError('invalid_role', 'Papel inválido.', 400)
    actor, target, superadmins = _begin(actor, 'roles.manage', target_id)
    _no_self(actor, target)
    old_role = target.role
    if old_role == new_role:
        raise AdminError('no_change', 'O utilizador já tem este papel.', 409)
    if (old_role, new_role) not in ROLE_TRANSITIONS:
        raise AdminError('invalid_transition', 'Transição de papel não permitida (passe por ADMIN).', 409)
    if new_role in (ROLE_ADMIN, ROLE_SUPERADMIN) and old_role == ROLE_USER and not target.is_active:
        raise AdminError('inactive_target', 'Ative a conta antes de a promover.', 409)
    if old_role == ROLE_SUPERADMIN and _is_last_active_superadmin(target, superadmins):
        raise AdminError('last_superadmin', 'Não é possível despromover o último SUPERADMIN ativo.', 409)
    verify_actor_password(actor, data.get('password'))
    target.role = new_role
    audit.record(actor, 'role.changed', 'success', 'user', target.id, _label(target),
                 {'from': old_role, 'to': new_role})
    db.session.commit()
    return target


def find_by_email(actor, email):
    _need(actor, 'roles.manage')
    email = (email or '').strip().lower()
    if not email or len(email) > 120:
        raise AdminError('invalid_email', 'Indique um email válido.', 400)
    user = User.query.filter_by(email=email).first()
    if user is None:
        raise AdminError('not_found', 'Utilizador não encontrado.', 404)
    return user


# ──────────────────────────────────────────────
# Operações destrutivas
# ──────────────────────────────────────────────
def data_summary(actor, target_id):
    _need(actor, 'users.clear_data')
    target = db.session.get(User, target_id)
    if target is None:
        raise AdminError('not_found', 'Utilizador não encontrado.', 404)
    return target, purge.user_counts(target.id)


def _mismatch_error(exc):
    extra = {'current_counts': exc.found} if isinstance(exc.found, dict) else {}
    return AdminError('counts_changed',
                      'Os dados mudaram desde a pré-visualização. Nada foi apagado; reveja as contagens.', 409, extra)


def clear_data(actor, target_id, data):
    actor, target, _ = _begin(actor, 'users.clear_data', target_id)
    _confirm_destructive(actor, target, data)
    label, tid = _label(target), target.id
    try:
        counts = purge.clear_user_data(tid, data.get('expected_counts'))
        audit.record(actor, 'user.data_cleared', 'success', 'user', tid, label, counts)
        db.session.commit()
    except purge.PurgeMismatch as exc:
        db.session.rollback()
        raise _mismatch_error(exc)
    return counts


def delete_user(actor, target_id, data):
    actor, target, superadmins = _begin(actor, 'users.delete', target_id)
    _no_self(actor, target)
    if _is_last_active_superadmin(target, superadmins):
        raise AdminError('last_superadmin', 'Não é possível excluir o último SUPERADMIN ativo.', 409)
    _confirm_destructive(actor, target, data)
    delete_tasks = data.get('delete_tasks') is True
    label, tid, role = _label(target), target.id, target.role
    try:
        counts = purge.delete_user(tid, data.get('expected_counts'), delete_tasks)
        audit.record(actor, 'user.deleted', 'success', 'user', tid, label,
                     dict(counts, delete_tasks=delete_tasks, role=role))
        db.session.commit()
    except purge.HasTasks as exc:
        db.session.rollback()
        raise AdminError('has_tasks', 'O utilizador tem tarefas. Escolha explicitamente apagá-las para excluir a conta.',
                         409, {'current_counts': exc.counts})
    except purge.PurgeMismatch as exc:
        db.session.rollback()
        raise _mismatch_error(exc)
    return counts
