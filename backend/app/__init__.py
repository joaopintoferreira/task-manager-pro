import os
import logging
from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_migrate import Migrate
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
#from config import Config, DevelopmentConfig, ProductionConfig
from backend.config import Config, DevelopmentConfig, ProductionConfig
#from app.models import db
from backend.app.models import db

migrate = Migrate()
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"],
    # Preflights CORS (OPTIONS) não consomem o limite: um 429 no preflight faz o navegador
    # bloquear a requisição real, e eles não carregam dados nem passam pela autenticação.
    default_limits_exempt_when=lambda: request.method == 'OPTIONS',
    storage_uri="memory://",
)


def create_app(config_class=None):
    app = Flask(__name__, instance_path='/tmp')

    # ── Configuração ──────────────────────────
    if config_class is None:
        db_url = os.environ.get('DATABASE_URL', '')
        #if db_url.startswith('postgresql'):
        if 'postgres' in db_url:
            config_class = ProductionConfig
        elif os.environ.get('VERCEL'):
            # O filesystem da Vercel é somente leitura: SQLite não é uma opção
            raise RuntimeError(
                "DATABASE_URL não definida (ou não é PostgreSQL). "
                "Configure a connection string do NeonDB nas variáveis de ambiente da Vercel."
            )
        else:
            config_class = DevelopmentConfig

    app.config.from_object(config_class)

    if not app.config.get('SECRET_KEY'):
        raise RuntimeError(
            "SECRET_KEY não definida. Configure a variável de ambiente SECRET_KEY."
        )

    # ── Logging ───────────────────────────────
    log_level = logging.DEBUG if app.config['DEBUG'] else logging.INFO
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )

    # ── Extensões ─────────────────────────────
    db.init_app(app)
    migrate.init_app(app, db)
    limiter.init_app(app)

    CORS(
        app,
        origins=app.config['CORS_ORIGINS'],
        supports_credentials=True,
        allow_headers=['Content-Type', 'Authorization'],
        methods=['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'],
    )

    # ── Blueprints ────────────────────────────
    from backend.app.auth import auth_bp
    from backend.app.routes.tasks import tasks_bp
    from backend.app.routes.categories import categories_bp
    from backend.app.routes.notifications import notifications_bp
    from backend.app.routes.ai import ai_bp
    from backend.app.routes.admin import admin_bp

    app.register_blueprint(auth_bp,          url_prefix='/auth')
    app.register_blueprint(tasks_bp,         url_prefix='/tasks')
    app.register_blueprint(categories_bp,    url_prefix='/categories')
    app.register_blueprint(notifications_bp, url_prefix='/notifications')
    app.register_blueprint(ai_bp,            url_prefix='/ai')
    app.register_blueprint(admin_bp,         url_prefix='/admin')

    from backend.app.cli import register_cli
    register_cli(app)

    # ── Cabeçalhos de segurança das respostas da API ──
    @app.after_request
    def security_headers(response):
        response.headers.setdefault('X-Content-Type-Options', 'nosniff')
        response.headers.setdefault('Referrer-Policy', 'no-referrer')
        if request.path.startswith(('/admin', '/auth')):
            response.headers.setdefault('Cache-Control', 'no-store')      # dados administrativos/sessões nunca em cache
        return response

    # ── Rota raiz ─────────────────────────────
    @app.route('/')
    def home():
        return jsonify({
            'message': 'Task Manager API',
            'version': '2.0.0',
            'status':  'running',
        })

    # ── Error handlers ────────────────────────
    @app.errorhandler(404)
    def not_found(e):
        return jsonify({'error': 'Endpoint não encontrado'}), 404

    @app.errorhandler(405)
    def method_not_allowed(e):
        return jsonify({'error': 'Método não permitido'}), 405

    @app.errorhandler(413)
    def payload_too_large(e):
        return jsonify({'message': 'Requisição muito grande (máximo 2 MB).'}), 413

    @app.errorhandler(429)
    def rate_limit_exceeded(e):
        return jsonify({'error': 'Muitas requisições. Aguarde e tente novamente.'}), 429

    @app.errorhandler(500)
    def internal_error(e):
        db.session.rollback()
        return jsonify({'error': 'Erro interno do servidor'}), 500


    # ── Criar tabelas se não existirem ────────
    #with app.app_context():
       # db.create_all()

    return app