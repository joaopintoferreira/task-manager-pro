import logging
from flask import Blueprint, request, jsonify
#from app.models import db, Category, Task
from backend.app.models import db, Category, Task
#from app.auth import token_required
from backend.app.auth import token_requiredS

logger = logging.getLogger(__name__)
categories_bp = Blueprint('categories', __name__)


@categories_bp.route('', methods=['GET'])
@token_required
def get_categories(current_user):
    categories = Category.query.filter_by(user_id=current_user.id).order_by(Category.name).all()
    return jsonify([c.to_dict() for c in categories])


@categories_bp.route('', methods=['POST'])
@token_required
def create_category(current_user):
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()

    if not name:
        return jsonify({'message': 'Nome da categoria é obrigatório!'}), 400

    # Evitar duplicatas por usuário
    if Category.query.filter_by(user_id=current_user.id, name=name).first():
        return jsonify({'message': 'Você já tem uma categoria com esse nome!'}), 400

    category = Category(
        name=name,
        color=data.get('color', '#6c757d'),
        user_id=current_user.id,
    )

    db.session.add(category)
    db.session.commit()

    logger.info(f"Categoria criada: {category.id} por user {current_user.id}")
    return jsonify({'message': 'Categoria criada!', 'category': category.to_dict()}), 201


@categories_bp.route('/<int:category_id>', methods=['PUT'])
@token_required
def update_category(current_user, category_id):
    category = Category.query.get_or_404(category_id)

    if category.user_id != current_user.id:
        return jsonify({'message': 'Acesso não autorizado!'}), 403

    data = request.get_json() or {}

    if 'name' in data:
        name = data['name'].strip()
        if not name:
            return jsonify({'message': 'Nome não pode ser vazio!'}), 400
        # Verificar conflito de nome (exceto a própria)
        existing = Category.query.filter_by(user_id=current_user.id, name=name).first()
        if existing and existing.id != category_id:
            return jsonify({'message': 'Você já tem uma categoria com esse nome!'}), 400
        category.name = name

    if 'color' in data:
        category.color = data['color']

    db.session.commit()
    return jsonify({'message': 'Categoria atualizada!', 'category': category.to_dict()})


@categories_bp.route('/<int:category_id>', methods=['DELETE'])
@token_required
def delete_category(current_user, category_id):
    category = Category.query.get_or_404(category_id)

    if category.user_id != current_user.id:
        return jsonify({'message': 'Acesso não autorizado!'}), 403

    # Desvincular tarefas (não deletar)
    Task.query.filter_by(category_id=category_id).update({'category_id': None})

    db.session.delete(category)
    db.session.commit()

    return jsonify({'message': 'Categoria excluída!'})
