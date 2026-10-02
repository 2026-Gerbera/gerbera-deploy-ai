"""SQLAlchemy Core storage shared by the application and explicit migration CLI."""

from flask import current_app, g
from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    event,
    func,
)

metadata = MetaData()
TABLE_OPTIONS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_unicode_ci",
}
user = Table(
    "user",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("username", String(255), unique=True, nullable=False),
    Column("password", String(255), nullable=False),
    **TABLE_OPTIONS,
)
post = Table(
    "post",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("author_id", Integer, ForeignKey("user.id"), nullable=False),
    Column("created", DateTime, nullable=False, server_default=func.current_timestamp()),
    Column("title", String(255), nullable=False),
    Column("body", Text, nullable=False),
    **TABLE_OPTIONS,
)
schema_version = Table(
    "schema_version",
    metadata,
    Column("version", String(4), primary_key=True),
    **TABLE_OPTIONS,
)


def make_engine(url):
    options = {"pool_pre_ping": True, "hide_parameters": True, "echo": False}
    if url.get_backend_name() == "mysql":
        options["connect_args"] = {"connect_timeout": 5, "read_timeout": 10, "write_timeout": 10}
    engine = create_engine(url, **options)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def configure_sqlite(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")

    return engine


def get_db():
    if "db" not in g:
        g.db = current_app.extensions["database"].connect()
    return g.db


def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()
