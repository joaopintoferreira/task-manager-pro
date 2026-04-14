import os
import logging
from flask import Flask, jsonify
from flask_cors import CORS
from flask_migrate import Migrate
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from config import Config, DevelopmentConfig, ProductionConfig
from app.models import db

migrate = Migrate()
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"],
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
        else:
            config_class = DevelopmentConfig

    app.config.from_object(config_class)

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
    from app.auth import auth_bp
    from app.routes.tasks import tasks_bp
    from app.routes.categories import categories_bp
    from app.routes.notifications import notifications_bp

    app.register_blueprint(auth_bp,          url_prefix='/auth')
    app.register_blueprint(tasks_bp,         url_prefix='/tasks')
    app.register_blueprint(categories_bp,    url_prefix='/categories')
    app.register_blueprint(notifications_bp, url_prefix='/notifications')

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