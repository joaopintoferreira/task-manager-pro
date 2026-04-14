from app import create_app

app = create_app()

# Vercel usa a variável `app` diretamente
if __name__ == '__main__':
    app.run()
