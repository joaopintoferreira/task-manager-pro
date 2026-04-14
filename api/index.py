from app import create_app

app = create_app()

def handler(request, response):
    return app(request, response)