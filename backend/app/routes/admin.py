"""
Painel administrativo (/admin/*).

TODAS as rotas passam por `require_permission` (autenticação + papel lido da base a cada pedido). A autorização
fina (alvo, último SUPERADMIN, confirmação de senha) está em backend.app.services.user_admin / maintenance.
Os registos de auditoria entram na mesma transação das operações; negações e falhas são gravadas à parte.
"""
import logging
from datetime import datetime, timedelta

from flask import Blueprint, request, jsonify

from backend.app import limiter
from backend.app.auth import generate_tokens
from backend.app.models import db, User, AdminAuditLog
from backend.app.services import audit, maintenance, purge, user_admin
from backend.app.services.permissions import AdminError, ROLES, has_permission, require_permission
from backend.app.utils import int_arg

logger = logging.getLogger(__name__)
admin_bp = Blueprint('admin', __name__)

READ_LIMIT = '120 per minute'
WRITE_LIMIT = '30 per minute'
DESTRUCTIVE_LIMIT = '10 per minute'
RESULTS = ('success', 'denied', 'failed')


def _body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _run(actor, action, fn, target_type='user', target_id=None):
    """Executa `fn()` convertendo AdminError em resposta e gravando negações/falhas na auditoria."""
    try:
        return fn(), None
    except AdminError as exc:
        db.session.rollback()
        if exc.status not in (400, 404) or exc.code == 'confirmation_mismatch':
            audit.record_isolated(actor, action, 'denied' if exc.status in (403, 429) else 'failed',
                                  target_type, target_id, None, {'code': exc.code})
        return None, exc.response()
    except Exception:
        db.session.rollback()
        logger.exception('Falha inesperada na operação administrativa %s', action)
        audit.record_isolated(actor, action, 'failed', target_type, target_id, None, {'code': 'internal_error'})
        return None, (jsonify({'message': 'Não foi possível concluir a operação. Nada foi alterado.',
                               'code': 'internal_error'}), 500)


def _page(query, serializer):
    page = int_arg(request.args.get('page'), 1, 1)
    per_page = int_arg(request.args.get('per_page'), 20, 1, 100)
    p = query.paginate(page=page, per_page=per_page, error_out=False)
    return {'items': [serializer(i) for i in p.items], 'total': p.total, 'page': p.page,
            'per_page': per_page, 'total_pages': p.pages}


def _escape_like(term):
    return term.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


# ──────────────────────────────────────────────
# Leitura
# ──────────────────────────────────────────────
@admin_bp.route('/overview', methods=['GET'])
@limiter.limit(READ_LIMIT)
@require_permission('admin.access')
def overview(current_user):
    count = lambda *c: User.query.filter(*c).count()
    return jsonify({
        'users': count(), 'active': count(User.is_active.is_(True)), 'inactive': count(User.is_active.isnot(True)),
        'admins': count(User.role == 'admin'), 'superadmins': count(User.role == 'superadmin'),
        'role': current_user.role,
    })


@admin_bp.route('/users', methods=['GET'])
@limiter.limit(READ_LIMIT)
@require_permission('users.list')
def list_users(current_user):
    query = User.query
    search = (request.args.get('search') or '').strip()[:100]
    if search:
        like = f'%{_escape_like(search)}%'
        query = query.filter(db.or_(User.username.ilike(like, escape='\\'), User.email.ilike(like, escape='\\')))
    role = request.args.get('role')
    if role in ROLES:
        query = query.filter(User.role == role)
    status = request.args.get('status')
    if status == 'active':
        query = query.filter(User.is_active.is_(True))
    elif status == 'inactive':
        query = query.filter(User.is_active.isnot(True))
    result = _page(query.order_by(User.id.asc()),
                   lambda u: dict(user_admin.admin_user_dict(u), actions=user_admin.allowed_actions(current_user, u)))
    return jsonify({'users': result['items'], **{k: v for k, v in result.items() if k != 'items'}})


@admin_bp.route('/users/<int:user_id>', methods=['GET'])
@limiter.limit(READ_LIMIT)
@require_permission('users.view')
def get_user(current_user, user_id):
    user = db.session.get(User, user_id)
    if user is None:
        return jsonify({'message': 'Utilizador não encontrado.', 'code': 'not_found'}), 404
    return jsonify({'user': dict(user_admin.admin_user_dict(user), actions=user_admin.allowed_actions(current_user, user))})


# ──────────────────────────────────────────────
# Operações simples
# ──────────────────────────────────────────────
@admin_bp.route('/users/<int:user_id>/activate', methods=['POST'])
@limiter.limit(WRITE_LIMIT)
@require_permission('users.list')
def activate_user(current_user, user_id):
    target, error = _run(current_user, 'user.activate', lambda: user_admin.set_active(current_user, user_id, True),
                         target_id=user_id)
    return error or jsonify({'message': 'Conta ativada.', 'user': user_admin.admin_user_dict(target)})


@admin_bp.route('/users/<int:user_id>/deactivate', methods=['POST'])
@limiter.limit(WRITE_LIMIT)
@require_permission('users.list')
def deactivate_user(current_user, user_id):
    target, error = _run(current_user, 'user.deactivate', lambda: user_admin.set_active(current_user, user_id, False),
                         target_id=user_id)
    return error or jsonify({'message': 'Conta desativada.', 'user': user_admin.admin_user_dict(target)})


@admin_bp.route('/users/<int:user_id>/avatar', methods=['DELETE'])
@limiter.limit(WRITE_LIMIT)
@require_permission('users.list')
def remove_user_avatar(current_user, user_id):
    target, error = _run(current_user, 'user.avatar_removed', lambda: user_admin.remove_avatar(current_user, user_id),
                         target_id=user_id)
    return error or jsonify({'message': 'Foto removida.', 'user': user_admin.admin_user_dict(target)})


# ──────────────────────────────────────────────
# Limpeza individual e exclusão (SUPERADMIN)
# ──────────────────────────────────────────────
@admin_bp.route('/users/<int:user_id>/data-summary', methods=['GET'])
@limiter.limit(READ_LIMIT)
@require_permission('users.clear_data')
def data_summary(current_user, user_id):
    result, error = _run(current_user, 'user.data_summary', lambda: user_admin.data_summary(current_user, user_id),
                         target_id=user_id)
    if error:
        return error
    target, counts = result
    return jsonify({'user': user_admin.admin_user_dict(target), 'counts': counts})


@admin_bp.route('/users/<int:user_id>/clear-data', methods=['POST'])
@limiter.limit(DESTRUCTIVE_LIMIT)
@require_permission('users.clear_data')
def clear_user_data(current_user, user_id):
    counts, error = _run(current_user, 'user.data_cleared', lambda: user_admin.clear_data(current_user, user_id, _body()),
                         target_id=user_id)
    return error or jsonify({'message': 'Dados do utilizador removidos. A conta foi mantida.', 'deleted': counts})


@admin_bp.route('/users/<int:user_id>', methods=['DELETE'])
@limiter.limit(DESTRUCTIVE_LIMIT)
@require_permission('users.delete')
def delete_user(current_user, user_id):
    counts, error = _run(current_user, 'user.deleted', lambda: user_admin.delete_user(current_user, user_id, _body()),
                         target_id=user_id)
    return error or jsonify({'message': 'Utilizador excluído.', 'deleted': counts})


# ──────────────────────────────────────────────
# Administradores e papéis (SUPERADMIN)
# ──────────────────────────────────────────────
@admin_bp.route('/admins', methods=['GET'])
@limiter.limit(READ_LIMIT)
@require_permission('roles.manage')
def list_admins(current_user):
    users = User.query.filter(User.role.in_(('admin', 'superadmin'))).order_by(User.id.asc()).all()
    return jsonify({'admins': [dict(user_admin.admin_user_dict(u), actions=user_admin.allowed_actions(current_user, u))
                               for u in users]})


@admin_bp.route('/users/lookup', methods=['GET'])
@limiter.limit(WRITE_LIMIT)
@require_permission('roles.manage')
def lookup_user(current_user):
    user, error = _run(current_user, 'role.lookup', lambda: user_admin.find_by_email(current_user, request.args.get('email')))
    return error or jsonify({'user': dict(user_admin.admin_user_dict(user),
                                          actions=user_admin.allowed_actions(current_user, user))})


@admin_bp.route('/admins', methods=['POST'])
@limiter.limit(DESTRUCTIVE_LIMIT)
@require_permission('roles.manage')
def promote_admin(current_user):
    data = _body()
    user_id = data.get('user_id')
    if isinstance(user_id, bool) or not isinstance(user_id, int):
        return jsonify({'message': 'Utilizador inválido.', 'code': 'invalid_request'}), 400
    target, error = _run(current_user, 'role.changed', lambda: user_admin.set_role(current_user, user_id, 'admin', data),
                         target_id=user_id)
    return error or jsonify({'message': 'Utilizador promovido a ADMIN.', 'user': user_admin.admin_user_dict(target)})


@admin_bp.route('/users/<int:user_id>/role', methods=['PUT'])
@limiter.limit(DESTRUCTIVE_LIMIT)
@require_permission('roles.manage')
def change_role(current_user, user_id):
    data = _body()
    target, error = _run(current_user, 'role.changed',
                         lambda: user_admin.set_role(current_user, user_id, data.get('role'), data), target_id=user_id)
    return error or jsonify({'message': 'Papel atualizado.', 'user': user_admin.admin_user_dict(target)})


# ──────────────────────────────────────────────
# Auditoria
# ──────────────────────────────────────────────
def _parse_bound(value, end_of_day=False):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None) - (dt.utcoffset() or timedelta(0))
    if end_of_day and len(str(value)) <= 10:
        dt += timedelta(days=1) - timedelta(microseconds=1)
    return dt


@admin_bp.route('/audit', methods=['GET'])
@limiter.limit(READ_LIMIT)
@require_permission('audit.view_own')
def list_audit(current_user):
    query = AdminAuditLog.query
    if has_permission(current_user, 'audit.view_all'):
        actor = request.args.get('actor_id')
        if actor and actor.isdigit() and int(actor) < 2 ** 31:
            query = query.filter(AdminAuditLog.actor_id == int(actor))
    else:
        query = query.filter(AdminAuditLog.actor_id == current_user.id)      # ADMIN: só as próprias ações
    action = (request.args.get('action') or '').strip()[:60]
    if action:
        query = query.filter(AdminAuditLog.action == action)
    result = request.args.get('result')
    if result in RESULTS:
        query = query.filter(AdminAuditLog.result == result)
    start, end = _parse_bound(request.args.get('from')), _parse_bound(request.args.get('to'), end_of_day=True)
    if start:
        query = query.filter(AdminAuditLog.created_at >= start)
    if end:
        query = query.filter(AdminAuditLog.created_at <= end)
    page = _page(query.order_by(AdminAuditLog.id.desc()), lambda e: e.to_dict())
    return jsonify({'entries': page['items'], **{k: v for k, v in page.items() if k != 'items'},
                    'scope': 'all' if has_permission(current_user, 'audit.view_all') else 'own'})


# ──────────────────────────────────────────────
# Manutenção e limpeza global (SUPERADMIN)
# ──────────────────────────────────────────────
@admin_bp.route('/maintenance/summary', methods=['GET'])
@limiter.limit(READ_LIMIT)
@require_permission('maintenance.view')
def maintenance_summary(current_user):
    return jsonify(maintenance.overview())


@admin_bp.route('/maintenance/wipe/preview', methods=['POST'])
@limiter.limit(DESTRUCTIVE_LIMIT)
@require_permission('maintenance.wipe')
def wipe_preview(current_user):
    result, error = _run(current_user, 'maintenance.wipe_preview', lambda: maintenance.wipe_preview(current_user),
                         target_type=None)
    return error or jsonify(result)


@admin_bp.route('/maintenance/wipe', methods=['POST'])
@limiter.limit('3 per minute')
@require_permission('maintenance.wipe')
def wipe(current_user):
    counts, error = _run(current_user, 'maintenance.wipe', lambda: maintenance.wipe_execute(current_user, _body()),
                         target_type=None)
    if error:
        return error
    # A limpeza apaga todos os refresh tokens; o administrador recebe tokens novos para não perder a sessão.
    # (generate_tokens faz o seu próprio commit, depois do commit da limpeza.)
    tokens = generate_tokens(current_user.id)
    return jsonify({'message': 'Dados operacionais removidos. Contas, esquema e auditoria foram preservados.',
                    'deleted': counts, 'tokens': tokens})
