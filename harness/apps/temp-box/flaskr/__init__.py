"""Flaskr, adapted from the Pallets Flask 3.1.2 tutorial (BSD-3-Clause)."""

from flask import Flask, jsonify, request
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.middleware.proxy_fix import ProxyFix

from . import auth, blog
from .config import app_config, database_url
from .db import close_db, make_engine
from .schema import SchemaError, validate_schema


def create_app():
    app = Flask(__name__)
    app.config.update(app_config())
    app.extensions["database"] = make_engine(database_url())
    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=app.config["PROXY_FIX_X_FOR"],
        x_proto=app.config["PROXY_FIX_X_PROTO"],
        x_host=0,
        x_port=0,
        x_prefix=0,
    )
    app.teardown_appcontext(close_db)
    app.register_blueprint(auth.bp)
    app.register_blueprint(blog.bp)
    app.add_url_rule("/", endpoint="index")

    @app.errorhandler(SQLAlchemyError)
    def database_error(_error):
        return jsonify(error="database_unavailable"), 503

    @app.get("/version")
    def version():
        return jsonify(release_id=app.config["RELEASE_ID"])

    @app.get("/health/ready")
    def ready():
        try:
            with app.extensions["database"].connect() as connection:
                current, signature = validate_schema(connection)
        except (SQLAlchemyError, SchemaError):
            return jsonify(ok=False, error="database_not_ready"), 503
        return jsonify(ok=True, current=current, signature=signature)

    @app.after_request
    def response_headers(response):
        if request.path in {"/version", "/health/ready"}:
            response.headers["Cache-Control"] = "no-store"
        return response

    return app
