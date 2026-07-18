from flask import Flask, render_template, request, redirect, url_for, session, flash
from datetime import datetime, timedelta
from db import db  # shared db connection
import os

# ===== Config / Defaults =====
PASSCODE_DEFAULT = "503860"  # <- your requested passcode


def create_app() -> Flask:
    app = Flask(__name__)

    # ---- Core config ----
    app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # Combined request limit for uploaded images
    app.config["JSON_SORT_KEYS"] = False
    app.secret_key = os.getenv("FLASK_SECRET_KEY", "change-this-in-production-please")  # 🔐

    # Basic session security (safe defaults)
    app.config.setdefault("SESSION_COOKIE_HTTPONLY", True)
    app.config.setdefault("SESSION_COOKIE_SAMESITE", "Lax")

    # expose db to blueprints
    app.mongo_db = db

    # ---- Blueprints ----
    from create_form import form_bp
    from admin import admin_bp
    from groups import groups_bp, public_groups_bp

    app.register_blueprint(form_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(groups_bp)
    app.register_blueprint(public_groups_bp)

    # ================= Auth helpers (inside app factory so they use this app) =================
    def _is_locked() -> bool:
        """
        Check if this session is temporarily locked out after too many failed attempts.
        """
        lu = session.get("locked_until")
        if not lu:
            return False
        try:
            return datetime.utcnow() < datetime.fromisoformat(lu)
        except Exception:
            # If stored value is bad, clear it and allow login attempts again
            session.pop("locked_until", None)
            return False

    def _register_fail() -> None:
        """
        Increment failed login attempts and, after 5 attempts,
        lock for 5 minutes.
        """
        attempts = int(session.get("login_attempts", 0)) + 1
        session["login_attempts"] = attempts
        if attempts >= 5:
            session["locked_until"] = (datetime.utcnow() + timedelta(minutes=5)).isoformat()
            session["login_attempts"] = 0

    def _reset_login_counters() -> None:
        """
        Clear lock + attempt counters after a successful login.
        """
        for k in ("login_attempts", "locked_until"):
            session.pop(k, None)

    def _safe_next(nxt: str | None) -> str:
        """
        Prevent open-redirect attacks. Only allow internal paths.
        """
        if nxt and isinstance(nxt, str) and nxt.startswith("/"):
            return nxt
        return url_for("admin_bp.dashboard")

    # ================= Routes =================

    @app.route("/", methods=["GET"])
    def index():
        if session.get("admin_logged_in"):
            return redirect(url_for("admin_bp.dashboard"))
        return redirect(url_for("login"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        # POST: attempting login
        if request.method == "POST":
            # Check if user is locked
            if _is_locked():
                flash("Too many attempts. Try again in 5 minutes.", "danger")
                return render_template("login.html")

            input_code = (request.form.get("passcode") or "").strip()
            required = os.getenv("ADMIN_PASSCODE", PASSCODE_DEFAULT)

            if input_code == required:
                session["admin_logged_in"] = True
                _reset_login_counters()
                nxt = _safe_next(request.args.get("next"))
                return redirect(nxt)

            # Wrong code
            _register_fail()
            flash("Invalid passcode. Please try again.", "danger")

        # GET or failed POST → show login
        return render_template("login.html")

    @app.route("/logout", methods=["POST", "GET"])
    def logout():
        session.clear()
        return redirect(url_for("login"))

    return app


# ===== WSGI entrypoint for gunicorn (Render) =====
# Render command: gunicorn app:app
app = create_app()


# ===== Local development entrypoint =====
if __name__ == "__main__":
    # Allow overriding port & debug via environment for flexibility
    debug = os.getenv("FLASK_DEBUG", "1") == "1"
    port = int(os.getenv("PORT", "5000"))
    app.run(debug=debug, host="0.0.0.0", port=port)
