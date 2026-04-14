import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

os.environ.setdefault('FLASK_ENV', 'production')

from app import create_app

app = create_app()