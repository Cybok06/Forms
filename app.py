from flask import Flask, render_template, request, redirect, url_for, session, flash
from datetime import datetime, timedelta
from db import db  # shared db connection
import os

PASSCODE_DEFAULT = "503860"  # <- your requested passcode

def create_app():
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50MB uploads to GridFS
    app.config["JSON_SORT_KEYS"] = False
    app.secret_key = os.getenv("FLASK_SECRET_KEY", "change-this-in-production-please")  # 🔐

    # expose db to blueprints
    app.mongo_db = db

    # Blueprints
    from create_form import form_bp
    from admin import admin_bp
    app.register_blueprint(form_bp)
    app.register_blueprint(admin_bp)

    # -------- Auth helpers --------
    def _is_locked():
        lu = session.get("locked_until")
        if not lu:
            return False
        try:
            return datetime.utcnow() < datetime.fromisoformat(lu)
        except Exception:
            session.pop("locked_until", None)
            return False

    def _register_fail():
        attempts = int(session.get("login_attempts", 0)) + 1
        session["login_attempts"] = attempts
        if attempts >= 5:
            # Lock for 5 minutes after 5 failed attempts
            session["locked_until"] = (datetime.utcnow() + timedelta(minutes=5)).isoformat()
            session["login_attempts"] = 0

    def _reset_login_counters():
        for k in ("login_attempts", "locked_until"):
            session.pop(k, None)

    def _safe_next(nxt):
        if nxt and isinstance(nxt, str) and nxt.startswith("/"):
            return nxt
        return url_for("admin_bp.dashboard")

    # -------- Routes --------
    @app.route("/", methods=["GET"])
    def index():
        if session.get("admin_logged_in"):
            return redirect(url_for("admin_bp.dashboard"))
        return redirect(url_for("login"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        # show lockout message if locked
        if request.method == "POST":
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

            _register_fail()
            flash("Invalid passcode. Please try again.", "danger")

        # GET or failed POST
        return render_template("login.html")

    @app.route("/logout", methods=["POST", "GET"])
    def logout():
        session.clear()
        return redirect(url_for("login"))

    return app

if __name__ == "__main__":
    app = create_app()
    app.run(debug=True)
