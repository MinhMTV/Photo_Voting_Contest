from flask import Flask
import os
from flask import request
from flask import session
from flask import url_for
import re
from dotenv import load_dotenv
from werkzeug.middleware.proxy_fix import ProxyFix


def create_app():
    app = Flask(__name__, instance_relative_config=True)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
    load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".flask_env"))
    load_dotenv()

    # Google OAuth requires HTTPS in production. For local development on
    # localhost/127.0.0.1 we allow insecure transport unless explicitly disabled.
    if os.getenv("OAUTHLIB_INSECURE_TRANSPORT") is None:
        flask_env = str(os.getenv("FLASK_ENV", "") or "").strip().lower()
        if flask_env != "production":
            os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

    app.config.from_mapping(
        ADMIN_PASSWORD=os.getenv("ADMIN_PASSWORD", "admin123"),
        SECRET_KEY=os.getenv("SECRET_KEY", "dev123"),
        PREFERRED_URL_SCHEME="https" if str(os.getenv("FLASK_ENV", "") or "").strip().lower() == "production" else "http",
        UPLOAD_FOLDER=os.path.join(app.root_path, "static/uploads"),
        DATABASE=os.path.join(app.instance_path, "votes.db"),
        BACKUP_FOLDER=os.path.join(os.path.dirname(app.root_path), "backups"),
    )

    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    os.makedirs(app.instance_path, exist_ok=True)
    os.makedirs(app.config["BACKUP_FOLDER"], exist_ok=True)

    from . import db

    db.init_app(app)
    with app.app_context():
        db.init_db()
        db.migrate_uploads_to_year_dirs(default_legacy_year=2025)
        db.migrate_null_years(default_legacy_year=2025)

    from . import routes

    app.register_blueprint(routes.bp)

    def contest_display_year(contest) -> str:
        if not contest:
            return ""
        value = contest.get("start_at") if hasattr(contest, "get") else None
        if value:
            match = re.match(r"^\s*(\d{4})", str(value))
            if match:
                return match.group(1)
        contest_id = contest.get("id") if hasattr(contest, "get") else None
        return str(contest_id or "")

    def contest_start_date(contest) -> str:
        if not contest:
            return ""
        value = str((contest.get("start_at") if hasattr(contest, "get") else "") or "").strip()
        match = re.match(r"^\s*(\d{4})-(\d{2})-(\d{2})", value)
        if match:
            return f"{match.group(3)}.{match.group(2)}.{match.group(1)}"
        return value

    def contest_label(contest) -> str:
        if not contest:
            return ""
        title = str((contest.get("title") if hasattr(contest, "get") else "") or "").strip()
        display_year = contest_display_year(contest)
        if title and display_year and display_year not in title:
            return f"{title} ({display_year})"
        return title or f"Contest {display_year}"

    def contest_admin_label(contest) -> str:
        if not contest:
            return ""
        label = contest_label(contest)
        contest_id = str((contest.get("id") if hasattr(contest, "get") else "") or "").strip()
        display_year = contest_display_year(contest)
        start_date = contest_start_date(contest)
        if start_date:
            label = f"{label} · Start {start_date}"
        if contest_id and contest_id != display_year:
            return f"{label} · ID {contest_id}"
        return label

    def contest_public_url(contest) -> str:
        if not contest:
            return "#"
        slug = str((contest.get("slug") if hasattr(contest, "get") else "") or "").strip()
        contest_id = contest.get("id") if hasattr(contest, "get") else None
        if slug:
            return url_for("main.contest_slug", slug=slug)
        return url_for("main.contest_year", year=contest_id)

    def contest_results_url(contest) -> str:
        if not contest:
            return "#"
        slug = str((contest.get("slug") if hasattr(contest, "get") else "") or "").strip()
        contest_id = contest.get("id") if hasattr(contest, "get") else None
        if slug:
            return url_for("main.public_results_slug", slug=slug)
        return url_for("main.public_results_year", year=contest_id)

    def contest_waiting_url(contest) -> str:
        if not contest:
            return "#"
        slug = str((contest.get("slug") if hasattr(contest, "get") else "") or "").strip()
        contest_id = contest.get("id") if hasattr(contest, "get") else None
        if slug:
            return url_for("main.public_waiting_slug", slug=slug)
        return url_for("main.public_waiting_preview", year=contest_id)

    @app.context_processor
    def inject_contest_helpers():
        return {
            "contest_display_year": contest_display_year,
            "contest_start_date": contest_start_date,
            "contest_label": contest_label,
            "contest_admin_label": contest_admin_label,
            "contest_public_url": contest_public_url,
            "contest_results_url": contest_results_url,
            "contest_waiting_url": contest_waiting_url,
        }

    return app

