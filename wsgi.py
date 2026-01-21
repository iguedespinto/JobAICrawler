"""WSGI entry point for Heroku."""

from app import create_app

app = create_app()
