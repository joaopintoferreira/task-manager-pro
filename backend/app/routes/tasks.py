import logging
from flask import Blueprint, request, jsonify
from datetime import datetime
#from app.models import db, Task, Category, TaskCollaborator, Notification, User
from backend.app.models import db, Task, Category, TaskCollaborator, Notification, User
#from app.auth import token_required
from backend.app.auth import token_required
#from app.utils import create_notification
from backend.app.utils import create_notification

logger = logging.getLogger(__name__)
tasks_bp = Blueprint('tasks', __name__)

VALID_PRIORITIES = {'high', 'medium', 'low'}
VALID_STATUSES   = {'pending', 'in_progress', 'completed'}
VALID_SORT_FIELDS = {'due_date', 'created_at', 'updated_at', 'priority', 'title'}


# ──────────────────────────────────────────────
# GET /tasks  — com filtros, busca, paginação e ordenação
# ──────────────────────────────────────────────
@tasks_bp.route('', methods=['GET'])
@token_required
def get_tasks(current_user):
    status      = request.args.get('status')
    priority    = request.args.get('priority')
    category_id = request.args.get('category_id')
    search      = request.args.get('search', '').strip()
    sort_by     = request.args.get('sort_by', 'created_at')
    order       = request.args.get('order', 'desc')
    page        = max(1, int(request.args.get('page', 1)))
    per_page    = min(100, max(1, int(request.args.get('per_page', 20))))

    query = Task.query.filter(
        db.or_(
            Task.user_id == current_user.id,
            Task.collaborators.any(user_id=current_user.id),
        )
    )

    if status and status in VALID_STATUSES:
        query = query.filter(Task.status == status)
    if priority and priority in VALID_PRIORITIES:
        query = query.filter(Task.priority == priority)
    if category_id:
        query = query.filter(Task.category_id == int(category_id))
    if search:
        pattern = f'%{search}%'
        query = query.filter(
            db.or_(Task.title.ilike(pattern), Task.description.ilike(pattern))
        )

    # Ordenação
    sort_col = sort_by if sort_by in VALID_SORT_FIELDS else 'created_at'
    col = getattr(Task, sort_col)
    query = query.order_by(col.desc() if order == 'desc' else col.asc())

    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    return jsonify({
        'tasks':       [t.to_dict() for t in pagination.items],
        'total':       pagination.total,
        'page':        pagination.page,
        'per_page':    per_page,
        'total_pages': pagination.pages,
        'has_next':    pagination.has_next,
        'has_prev':    pagination.has_prev,
    })


# ──────────────────────────────────────────────
# POST /tasks
# ──────────────────────────────────────────────
@tasks_bp.route('', methods=['POST'])
@token_required
def create_task(current_user):
    data = request.get_json() or {}

    title = (data.get('title') or '').strip()
    if not title:
        return jsonify({'message': 'Título é obrigatório!'}), 400

    priority = data.get('priority', 'medium')
    status   = data.get('status',   'pending')

    if priority not in VALID_PRIORITIES:
        return jsonify({'message': f'Prioridade inválida. Use: {", ".join(VALID_PRIORITIES)}'}), 400
    if status not in VALID_STATUSES:
        return jsonify({'message': f'Status inválido. Use: {", ".join(VALID_STATUSES)}'}), 400

    due_date = None
    if data.get('due_date'):
        try:
            due_date = datetime.fromisoformat(data['due_date'])
        except ValueError:
            return jsonify({'message': 'Formato de data inválido (use ISO 8601)'}), 400

    task = Task(
        title=title,
        description=(data.get('description') or '').strip(),
        due_date=due_date,
        priority=priority,
        status=status,
        user_id=current_user.id,
        category_id=data.get('category_id') or None,
    )

    db.session.add(task)
    db.session.commit()

    create_notification(
        user_id=current_user.id,
        title='Tarefa criada',
        message=f"Tarefa '{task.title}' criada com sucesso.",
        notification_type='task_created',
        task_id=task.id,
    )

    logger.info(f"Tarefa criada: {task.id} por user {current_user.id}")
    return jsonify({'message': 'Tarefa criada com sucesso!', 'task': task.to_dict()}), 201


# ──────────────────────────────────────────────
# GET /tasks/<id>
# ──────────────────────────────────────────────
@tasks_bp.route('/<int:task_id>', methods=['GET'])
@token_required
def get_task(current_user, task_id):
    task = Task.query.get_or_404(task_id)
    if not task.can_access(current_user.id):
        return jsonify({'message': 'Acesso não autorizado!'}), 403
    return jsonify(task.to_dict())


# ──────────────────────────────────────────────
# PUT /tasks/<id>
# ──────────────────────────────────────────────
@tasks_bp.route('/<int:task_id>', methods=['PUT'])
@token_required
def update_task(current_user, task_id):
    task = Task.query.get_or_404(task_id)
    if not task.can_access(current_user.id):
        return jsonify({'message': 'Acesso não autorizado!'}), 403

    data = request.get_json() or {}

    if 'title' in data:
        title = data['title'].strip()
        if not title:
            return jsonify({'message': 'Título não pode ser vazio!'}), 400
        task.title = title

    if 'description' in data:
        task.description = (data['description'] or '').strip()

    if 'due_date' in data:
        if data['due_date']:
            try:
                task.due_date = datetime.fromisoformat(data['due_date'])
            except ValueError:
                return jsonify({'message': 'Formato de data inválido'}), 400
        else:
            task.due_date = None

    if 'priority' in data:
        if data['priority'] not in VALID_PRIORITIES:
            return jsonify({'message': 'Prioridade inválida'}), 400
        task.priority = data['priority']

    if 'status' in data:
        if data['status'] not in VALID_STATUSES:
            return jsonify({'message': 'Status inválido'}), 400
        old_status = task.status
        task.status = data['status']
        if old_status != 'completed' and data['status'] == 'completed':
            create_notification(
                user_id=current_user.id,
                title='Tarefa concluída 🎉',
                message=f"Parabéns! Você concluiu a tarefa '{task.title}'.",
                notification_type='task_completed',
                task_id=task.id,
            )

    if 'category_id' in data:
        task.category_id = data['category_id'] or None

    task.updated_at = datetime.utcnow()
    db.session.commit()

    return jsonify({'message': 'Tarefa atualizada com sucesso!', 'task': task.to_dict()})


# ──────────────────────────────────────────────
# DELETE /tasks/<id>
# ──────────────────────────────────────────────
@tasks_bp.route('/<int:task_id>', methods=['DELETE'])
@token_required
def delete_task(current_user, task_id):
    task = Task.query.get_or_404(task_id)
    if task.user_id != current_user.id:
        return jsonify({'message': 'Apenas o dono pode excluir a tarefa!'}), 403

    task_title = task.title
    db.session.delete(task)
    db.session.commit()

    create_notification(
        user_id=current_user.id,
        title='Tarefa excluída',
        message=f"A tarefa '{task_title}' foi excluída.",
        notification_type='task_deleted',
    )

    logger.info(f"Tarefa {task_id} excluída por user {current_user.id}")
    return jsonify({'message': 'Tarefa excluída com sucesso!'})


# ──────────────────────────────────────────────
# POST /tasks/<id>/collaborators  — adicionar colaborador
# ──────────────────────────────────────────────
@tasks_bp.route('/<int:task_id>/collaborators', methods=['POST'])
@token_required
def add_collaborator(current_user, task_id):
    task = Task.query.get_or_404(task_id)
    if task.user_id != current_user.id:
        return jsonify({'message': 'Apenas o dono pode adicionar colaboradores!'}), 403

    data  = request.get_json() or {}
    email = (data.get('email') or '').strip().lower()
    role  = data.get('role', 'viewer')

    if not email:
        return jsonify({'message': 'Email do colaborador é obrigatório!'}), 400
    if role not in {'viewer', 'editor'}:
        return jsonify({'message': 'Role inválida. Use viewer ou editor.'}), 400

    target = User.query.filter_by(email=email).first()
    if not target:
        return jsonify({'message': 'Usuário não encontrado!'}), 404
    if target.id == current_user.id:
        return jsonify({'message': 'Você já é o dono da tarefa!'}), 400

    existing = TaskCollaborator.query.filter_by(task_id=task_id, user_id=target.id).first()
    if existing:
        return jsonify({'message': 'Usuário já é colaborador desta tarefa!'}), 400

    collab = TaskCollaborator(task_id=task_id, user_id=target.id, role=role)
    db.session.add(collab)

    create_notification(
        user_id=target.id,
        title='Nova colaboração',
        message=f"{current_user.username} adicionou você à tarefa '{task.title}'.",
        notification_type='task_assignment',
        task_id=task_id,
    )

    db.session.commit()
    return jsonify({'message': 'Colaborador adicionado!', 'collaborator': collab.to_dict()}), 201


# ──────────────────────────────────────────────
# DELETE /tasks/<id>/collaborators/<user_id>
# ──────────────────────────────────────────────
@tasks_bp.route('/<int:task_id>/collaborators/<int:user_id>', methods=['DELETE'])
@token_required
def remove_collaborator(current_user, task_id, user_id):
    task = Task.query.get_or_404(task_id)
    if task.user_id != current_user.id:
        return jsonify({'message': 'Apenas o dono pode remover colaboradores!'}), 403

    collab = TaskCollaborator.query.filter_by(task_id=task_id, user_id=user_id).first_or_404()
    db.session.delete(collab)
    db.session.commit()
    return jsonify({'message': 'Colaborador removido!'})


# ──────────────────────────────────────────────
# GET /tasks/stats  — estatísticas do usuário
# ──────────────────────────────────────────────
@tasks_bp.route('/stats', methods=['GET'])
@token_required
def get_stats(current_user):
    tasks = Task.query.filter_by(user_id=current_user.id).all()
    now   = datetime.utcnow()

    total       = len(tasks)
    completed   = sum(1 for t in tasks if t.status == 'completed')
    in_progress = sum(1 for t in tasks if t.status == 'in_progress')
    pending     = sum(1 for t in tasks if t.status == 'pending')
    overdue     = sum(1 for t in tasks if t.due_date and t.due_date < now and t.status != 'completed')

    return jsonify({
        'total':           total,
        'completed':       completed,
        'in_progress':     in_progress,
        'pending':         pending,
        'overdue':         overdue,
        'completion_rate': round(completed / total * 100, 1) if total else 0,
        'priority_breakdown': {
            'high':   sum(1 for t in tasks if t.priority == 'high'   and t.status != 'completed'),
            'medium': sum(1 for t in tasks if t.priority == 'medium' and t.status != 'completed'),
            'low':    sum(1 for t in tasks if t.priority == 'low'    and t.status != 'completed'),
        },
    })
