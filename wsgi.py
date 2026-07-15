"""WSGI entry point, used by ``flask --app wsgi run``."""

from app import create_app

app = create_app()
