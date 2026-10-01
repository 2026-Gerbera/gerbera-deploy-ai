from flask import Blueprint, flash, g, redirect, render_template, request, url_for
from sqlalchemy import select
from werkzeug.exceptions import abort

from .auth import login_required
from .db import get_db
from .db import post as posts_table
from .db import user as users

bp = Blueprint("blog", __name__)

POSTS = select(posts_table, users.c.username).join(users, posts_table.c.author_id == users.c.id)


@bp.route("/")
def index():
    """Show all the posts, most recent first."""
    db = get_db()
    posts = (
        db.execute(POSTS.order_by(posts_table.c.created.desc(), posts_table.c.id.desc()))
        .mappings()
        .all()
    )
    return render_template("blog/index.html", posts=posts)


def get_post(id, check_author=True):
    """Get a post and its author by id.

    Checks that the id exists and optionally that the current user is
    the author.

    :param id: id of post to get
    :param check_author: require the current user to be the author
    :return: the post with author information
    :raise 404: if a post with the given id doesn't exist
    :raise 403: if the current user isn't the author
    """
    post = (
        get_db()
        .execute(
            POSTS.where(posts_table.c.id == id),
        )
        .mappings()
        .first()
    )

    if post is None:
        abort(404, f"Post id {id} doesn't exist.")

    if check_author and post["author_id"] != g.user["id"]:
        abort(403)

    return post


@bp.route("/create", methods=("GET", "POST"))
@login_required
def create():
    """Create a new post for the current user."""
    if request.method == "POST":
        title = request.form["title"]
        body = request.form["body"]
        error = None

        if not title:
            error = "Title is required."
        elif len(title) > 255:
            error = "Title is too long."

        if error is not None:
            flash(error)
        else:
            db = get_db()
            db.execute(
                posts_table.insert().values(title=title, body=body, author_id=g.user["id"]),
            )
            db.commit()
            return redirect(url_for("blog.index"))

    return render_template("blog/create.html")


@bp.route("/<int:id>/update", methods=("GET", "POST"))
@login_required
def update(id):
    """Update a post if the current user is the author."""
    post = get_post(id)

    if request.method == "POST":
        title = request.form["title"]
        body = request.form["body"]
        error = None

        if not title:
            error = "Title is required."
        elif len(title) > 255:
            error = "Title is too long."

        if error is not None:
            flash(error)
        else:
            db = get_db()
            db.execute(
                posts_table.update().where(posts_table.c.id == id).values(title=title, body=body)
            )
            db.commit()
            return redirect(url_for("blog.index"))

    return render_template("blog/update.html", post=post)


@bp.route("/<int:id>/delete", methods=("POST",))
@login_required
def delete(id):
    """Delete a post.

    Ensures that the post exists and that the logged in user is the
    author of the post.
    """
    get_post(id)
    db = get_db()
    db.execute(posts_table.delete().where(posts_table.c.id == id))
    db.commit()
    return redirect(url_for("blog.index"))
