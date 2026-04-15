import logging
from flask import Blueprint, request, jsonify, current_app
from functools import wraps
import jwt
from datetime import datetime, timedelta
#from app.models import db, User, RefreshToken
from backend.app.models import db, User, RefreshToken
from app.utils import validate_password, validate_email
from app import limiter

logger = logging.getLogger(__name__)
auth_bp = Blueprint('auth', __name__)

# ──────────────────────────────────────────────
# Decorator de autenticação
# ──────────────────────────────────────────────
def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        auth_header = request.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            token = auth_header.split(' ')[1]

        if not token:
            return jsonify({'message': 'Token ausente!'}), 401

        try:
            data = jwt.decode(token, current_app.config['SECRET_KEY'], algorithms=['HS256'])
            current_user = User.query.get(data['user_id'])
            if not current_user or not current_user.is_active:
                return jsonify({'message': 'Usuário não encontrado ou inativo!'}), 401
        except jwt.ExpiredSignatureError:
            return jsonify({'message': 'Token expirado!'}), 401
        except jwt.InvalidTokenError:
            return jsonify({'message': 'Token inválido!'}), 401
        except Exception as e:
            logger.error(f"Erro de autenticação: {e}")
            return jsonify({'message': 'Erro de autenticação!'}), 401

        return f(current_user, *args, **kwargs)
    return decorated


# ──────────────────────────────────────────────
# Geração de tokens
# ──────────────────────────────────────────────
def generate_tokens(user_id):
    access_token = jwt.encode(
        {
            'user_id': user_id,
            'exp': datetime.utcnow() + current_app.config['JWT_ACCESS_TOKEN_EXPIRES'],
        },
        current_app.config['SECRET_KEY'],
        algorithm='HS256',
    )

    refresh_token = jwt.encode(
        {
            'user_id': user_id,
            'exp': datetime.utcnow() + current_app.config['JWT_REFRESH_TOKEN_EXPIRES'],
        },
        current_app.config['SECRET_KEY'],
        algorithm='HS256',
    )

    # Limpar tokens antigos do usuário (evitar acúmulo)
    RefreshToken.query.filter_by(user_id=user_id).filter(
        RefreshToken.expires_at < datetime.utcnow()
    ).delete()

    db.session.add(RefreshToken(
        user_id=user_id,
        token=refresh_token,
        expires_at=datetime.utcnow() + current_app.config['JWT_REFRESH_TOKEN_EXPIRES'],
    ))
    db.session.commit()

    return {
        'access_token':  access_token,
        'refresh_token': refresh_token,
        'expires_in':    int(current_app.config['JWT_ACCESS_TOKEN_EXPIRES'].total_seconds()),
    }


# ──────────────────────────────────────────────
# Rotas
# ──────────────────────────────────────────────
@auth_bp.route('/register', methods=['POST'])
@limiter.limit("10 per hour")
def register():
    data = request.get_json() or {}

    email    = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''
    username = (data.get('username') or '').strip()

    # Validações
    if not email or not password:
        return jsonify({'message': 'Email e senha são obrigatórios!'}), 400

    if not validate_email(email):
        return jsonify({'message': 'Formato de email inválido!'}), 400

    is_valid, msg = validate_password(password)
    if not is_valid:
        return jsonify({'message': msg}), 400

    if User.query.filter_by(email=email).first():
        return jsonify({'message': 'Email já cadastrado!'}), 400

    if username and User.query.filter_by(username=username).first():
        return jsonify({'message': 'Nome de usuário já existe!'}), 400

    user = User(
        username=username or email.split('@')[0],
        email=email,
    )
    user.set_password(password)

    db.session.add(user)
    db.session.commit()

    logger.info(f"Novo usuário registrado: {email}")
    tokens = generate_tokens(user.id)

    return jsonify({
        'message': 'Usuário registrado com sucesso!',
        'user':    user.to_dict(),
        'tokens':  tokens,
    }), 201


@auth_bp.route('/login', methods=['POST'])
@limiter.limit("5 per minute")
def login():
    data = request.get_json() or {}

    email    = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''

    if not email or not password:
        return jsonify({'message': 'Email e senha são obrigatórios!'}), 400

    user = User.query.filter_by(email=email).first()

    # Mesmo delay para email inexistente (evitar user enumeration)
    if not user or not user.check_password(password):
        logger.warning(f"Tentativa de login falhou para: {email}")
        return jsonify({'message': 'Credenciais inválidas!'}), 401

    if not user.is_active:
        return jsonify({'message': 'Conta desativada. Entre em contato com o suporte.'}), 403

    user.last_login = datetime.utcnow()
    db.session.commit()

    logger.info(f"Login bem-sucedido: {email}")
    tokens = generate_tokens(user.id)

    return jsonify({
        'message': 'Login realizado com sucesso!',
        'user':    user.to_dict(),
        'tokens':  tokens,
    })


@auth_bp.route('/refresh', methods=['POST'])
def refresh():
    data          = request.get_json() or {}
    refresh_token = data.get('refresh_token')

    if not refresh_token:
        return jsonify({'message': 'Refresh token é obrigatório!'}), 400

    try:
        payload = jwt.decode(
            refresh_token,
            current_app.config['SECRET_KEY'],
            algorithms=['HS256'],
        )

        stored = RefreshToken.query.filter_by(
            token=refresh_token,
            user_id=payload['user_id'],
        ).first()

        if not stored or stored.expires_at < datetime.utcnow():
            return jsonify({'message': 'Refresh token inválido ou expirado!'}), 401

        db.session.delete(stored)
        tokens = generate_tokens(payload['user_id'])

        return jsonify({
            'message': 'Tokens atualizados com sucesso!',
            'tokens':  tokens,
        })

    except jwt.InvalidTokenError:
        return jsonify({'message': 'Refresh token inválido!'}), 401


@auth_bp.route('/logout', methods=['POST'])
@token_required
def logout(current_user):
    data          = request.get_json() or {}
    refresh_token = data.get('refresh_token')

    if refresh_token:
        RefreshToken.query.filter_by(
            token=refresh_token,
            user_id=current_user.id,
        ).delete()
        db.session.commit()

    logger.info(f"Logout: {current_user.email}")
    return jsonify({'message': 'Logout realizado com sucesso!'})


@auth_bp.route('/me', methods=['GET'])
@token_required
def me(current_user):
    """Retorna os dados do usuário autenticado."""
    return jsonify({'user': current_user.to_dict()})
