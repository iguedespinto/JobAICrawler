"""Gunicorn configuration for Heroku."""

import os

bind = f"0.0.0.0:{os.getenv('PORT', '5000')}"
workers = int(os.getenv("WEB_CONCURRENCY", "2"))
threads = int(os.getenv("GUNICORN_THREADS", "4"))
timeout = int(os.getenv("GUNICORN_TIMEOUT", "60"))
loglevel = os.getenv("GUNICORN_LOGLEVEL", "info")
accesslog = "-"
errorlog = "-"
