"""Flask app factory and shared extensions."""

from __future__ import annotations

import os
from typing import Any, Optional

from flask import Flask, current_app, g
from dotenv import load_dotenv
from pymongo import MongoClient


def create_app() -> Flask:
    """Create and configure the Flask application."""
    load_dotenv()
    app = Flask(__name__)
    _load_config(app)
    _init_mongo(app)
    _register_blueprints(app)

    @app.route("/")
    def index():
        """Send the root URL to the jobs list."""
        from flask import redirect, url_for

        return redirect(url_for("jobs.list_jobs"))

    @app.route("/healthz")
    def healthcheck() -> dict:
        """Simple health check for uptime monitoring."""
        return {"status": "ok"}

    return app


def _load_config(app: Flask) -> None:
    """Load configuration from environment variables."""
    app.config["MONGODB_URI"] = os.getenv("MONGODB_URI", "") or os.getenv("MONGO_URI", "")
    app.config["MONGO_DB_NAME"] = os.getenv("MONGO_DB_NAME", "jobs_db")
    # Used to sign the session cookie that backs flashed messages.
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-key")


def _init_mongo(app: Flask) -> None:
    """Initialize Mongo client and store in Flask extensions."""
    mongo_uri = app.config["MONGODB_URI"]
    if not mongo_uri:
        # Allow local development without a DB connection.
        app.extensions["mongo_client"] = None
        return

    app.extensions["mongo_client"] = MongoClient(mongo_uri)


def get_db() -> Optional[Any]:
    """Get the MongoDB database handle for the current request."""
    if "db" not in g:
        client: Optional[MongoClient] = current_app.extensions.get("mongo_client")
        if client is None:
            g.db = None
        else:
            g.db = client[current_app.config["MONGO_DB_NAME"]]
    return g.db


def _register_blueprints(app: Flask) -> None:
    """Register application blueprints."""
    from .routes_jobs import jobs_bp
    from .routes_profile import profile_bp
    from .routes_import import import_bp

    app.register_blueprint(jobs_bp)
    app.register_blueprint(profile_bp)
    app.register_blueprint(import_bp)
