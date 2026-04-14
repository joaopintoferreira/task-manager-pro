import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from app import create_app

app = create_app()

@app.route('/debug-env')
def debug_env():
    from flask import jsonify
    return jsonify({
        'DATABASE_URL': os.environ.get('DATABASE_URL', 'NAO ENCONTRADA'),
        'FLASK_ENV': os.environ.get('FLASK_ENV', 'NAO ENCONTRADA'),
        'SECRET_KEY': 'existe' if os.environ.get('SECRET_KEY') else 'NAO ENCONTRADA',
    })

if __name__ == '__main__':
    app.run()