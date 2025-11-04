# app.py
from flask import Flask, render_template, request, redirect, url_for, session, flash
from db import db  # shared db connection
import os

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

    # -------- Auth routes (simple hardcoded admin) --------
    @app.route("/", methods=["GET"])
    def index():
        # Always start at login
        return redirect(url_for("login"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            password = (request.form.get("password") or "").strip()
            if username == "admin" and password == "Admin@2025":
                session["admin_logged_in"] = True
                nxt = request.args.get("next") or url_for("admin_bp.dashboard")
                return redirect(nxt)
            flash("Invalid credentials. Try again.", "danger")
        return render_template("login.html")

    @app.route("/logout", methods=["POST", "GET"])
    def logout():
        session.clear()
        return redirect(url_for("login"))

    return app

if __name__ == "__main__":
    app = create_app()
    app.run(debug=True)
