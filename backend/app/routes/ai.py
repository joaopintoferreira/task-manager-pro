"""
POST /ai/chat — Assistente de IA (somente orienta: não executa nenhuma ação no sistema).

A rota trata de autenticação, validação e resposta HTTP; a comunicação com o provedor e o prompt
ficam em backend.app.ai_service. Nada é persistido e nada do conteúdo é registado nos logs.
"""
import logging
import time
from datetime import datetime
from flask import Blueprint, request, jsonify, current_app

from backend.app import limiter
from backend.app import ai_service
from backend.app.auth import token_required
from backend.app.models import db, Task, iso_utc

logger = logging.getLogger(__name__)
ai_bp = Blueprint('ai', __name__)

MAX_MESSAGES = 10          # mensagens de histórico aceites
MAX_MESSAGE_CHARS = 2000   # por mensagem
MAX_TOTAL_CHARS = 8000     # histórico inteiro
MAX_DESCRIPTION_CHARS = 1500   # descrição da tarefa enviada como contexto
MAX_TITLE_CHARS = 200
ALLOWED_ROLES = {'user', 'assistant'}   # `system` nunca vem do cliente


def _invalid(message):
    return jsonify({'message': message, 'code': 'invalid_request'}), 400


def _validate_history(data):
    """Devolve (histórico, None) ou (None, resposta_de_erro)."""
    messages = data.get('messages')
    if not isinstance(messages, list) or not messages:
        return None, _invalid('Envie ao menos uma mensagem.')
    if len(messages) > MAX_MESSAGES:
        return None, _invalid(f'O histórico aceita no máximo {MAX_MESSAGES} mensagens.')

    history, total = [], 0
    for m in messages:
        if not isinstance(m, dict) or m.get('role') not in ALLOWED_ROLES or not isinstance(m.get('content'), str):
            return None, _invalid('Mensagem inválida.')
        content = m['content'].strip()
        if not content:
            return None, _invalid('As mensagens não podem estar vazias.')
        if len(content) > MAX_MESSAGE_CHARS:
            return None, _invalid(f'Cada mensagem aceita no máximo {MAX_MESSAGE_CHARS} caracteres.')
        total += len(content)
        history.append({'role': m['role'], 'content': content})
    if total > MAX_TOTAL_CHARS:
        return None, _invalid(f'O histórico aceita no máximo {MAX_TOTAL_CHARS} caracteres.')
    if history[-1]['role'] != 'user':
        return None, _invalid('A última mensagem deve ser do utilizador.')
    return history, None


def _user_role(task, user_id):
    if task.user_id == user_id:
        return 'proprietário'
    mine = task.collaborators.filter_by(user_id=user_id).first()
    return 'editor' if mine and mine.role == 'editor' else 'leitor'


def _task_context(task_id, user):
    """Contexto mínimo da tarefa, após verificar o acesso NESTE pedido. None se não houver acesso."""
    task = db.session.get(Task, task_id)
    # Inexistente e sem acesso respondem igual (404), para não revelar que o ID existe
    if task is None or not task.can_access(user.id):
        return None, None
    context = {
        'titulo': (task.title or '')[:MAX_TITLE_CHARS],
        'descricao': (task.description or '')[:MAX_DESCRIPTION_CHARS],
        'estado': task.status,
        'prioridade': task.priority,
        'prazo_utc': iso_utc(task.due_date),
        'atrasada': bool(task.due_date and task.status != 'completed' and task.due_date < datetime.utcnow()),
        'categoria': task.category.name if task.category else None,
        'papel_do_utilizador': _user_role(task, user.id),
    }
    return context, {'task_id': task.id, 'title': context['titulo']}


@ai_bp.route('/chat', methods=['POST'])
@limiter.limit('10 per minute;100 per hour')
@token_required
def chat(current_user):
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _invalid('Corpo da requisição inválido.')

    history, error = _validate_history(data)
    if error:
        return error

    task_id = data.get('task_id')
    if task_id is not None and (isinstance(task_id, bool) or not isinstance(task_id, int) or task_id < 1):
        return _invalid('Tarefa inválida.')

    try:
        cfg = ai_service.load_config(current_app.config)
    except ai_service.AIError as exc:
        _log(current_user, None, exc.http_status, 0, exc.code, False)
        return jsonify({'message': exc.public_message, 'code': exc.code}), exc.http_status

    task_context, context_info = None, None
    if task_id is not None:
        task_context, context_info = _task_context(task_id, current_user)
        if task_context is None:
            return jsonify({'message': 'Tarefa não encontrada.', 'code': 'task_not_found'}), 404

    started = time.monotonic()
    try:
        reply = ai_service.chat(cfg, ai_service.build_messages(history, task_context))
    except ai_service.AIError as exc:
        _log(current_user, cfg.provider, exc.http_status, started, exc.code, task_context is not None)
        return jsonify({'message': exc.public_message, 'code': exc.code}), exc.http_status

    _log(current_user, cfg.provider, 200, started, None, task_context is not None)
    return jsonify({'reply': reply, 'context': context_info})


def _log(user, provider, status, started, error, with_context):
    """Só metadados: nunca chave, mensagens, prompts, respostas nem conteúdo de tarefas."""
    latency_ms = int((time.monotonic() - started) * 1000) if started else 0
    logger.info('ai_chat user=%s provider=%s status=%s latency_ms=%s error=%s task_context=%s',
                user.id, provider or '-', status, latency_ms, error or '-', with_context)
