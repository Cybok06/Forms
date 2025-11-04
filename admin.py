from flask import (
    Blueprint, render_template, request, current_app, abort,
    send_file, jsonify, url_for, redirect, session
)
from datetime import datetime
from bson import ObjectId
import io, math, re

# Optional export libs
try:
    import pandas as pd
except Exception:
    pd = None

try:
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet
except Exception:
    pass

admin_bp = Blueprint("admin_bp", __name__, url_prefix="/admin")

# ----------------- helpers -----------------
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TEL_RE = re.compile(r"^[0-9+()\-.\s]{3,}$")  # permissive

@admin_bp.before_request
def _require_login():
    if not session.get("admin_logged_in"):
        return redirect(url_for("login", next=request.url))

def _int(val, default):
    try: return int(val)
    except Exception: return default

def _format_dt(dt):
    if not dt: return ""
    return dt.strftime("%Y-%m-%d %H:%M")

def _field_map(form):
    return {f.get("id"): f for f in (form.get("fields") or []) if f.get("id")}

def _field_order(form):
    return [f.get("id") for f in (form.get("fields") or []) if f.get("id")]

def _build_dataframe_without_submitted_at(form, submissions, include_no=True):
    """
    Excel export helper: builds a DataFrame with ONLY the field columns
    (omits 'Submitted At'). If include_no=True, prepends 'No' column 1..N.
    """
    if pd is None:
        return None
    cols = _field_order(form)
    headers = [next((f.get("label") for f in form.get("fields", []) if f.get("id")==cid), cid) for cid in cols]
    rows = []
    for s in submissions:
        rows.append([s.get("fields", {}).get(cid, "") for cid in cols])
    df = pd.DataFrame(rows, columns=headers)
    if include_no:
        df.insert(0, "No", range(1, len(df) + 1))
    return df

# ----------------- routes -----------------
@admin_bp.route("/", methods=["GET"])
def dashboard():
    db = current_app.mongo_db
    forms = db["forms"]
    submissions = db["submissions"]

    q = (request.args.get("q") or "").strip()
    page = _int(request.args.get("page"), 1)
    per_page = min(max(_int(request.args.get("per_page"), 10), 5), 50)

    find_query = {"title": {"$regex": re.escape(q), "$options": "i"}} if q else {}
    total_forms = forms.count_documents({})
    total_match = forms.count_documents(find_query)
    cursor = forms.find(find_query).sort("created_at", -1).skip((page-1)*per_page).limit(per_page)
    rows = list(cursor)

    slugs = [r["slug"] for r in rows]
    counts_by_slug = {s["_id"]: s["count"] for s in submissions.aggregate([
        {"$match": {"slug": {"$in": slugs}}},
        {"$group": {"_id": "$slug", "count": {"$sum": 1}}}
    ])}

    for r in rows:
        r["_id"] = str(r["_id"])
        r["created_at_str"] = _format_dt(r.get("created_at"))
        r["submissions_count"] = counts_by_slug.get(r["slug"], 0)
        r["suspended"] = bool(r.get("suspended", False))  # ensure key present

    pages = math.ceil(total_match / per_page) if per_page else 1
    return render_template(
        "admin_dashboard.html",
        total_forms=total_forms,
        q=q, page=page, per_page=per_page, pages=pages,
        forms_list=rows
    )

@admin_bp.route("/forms/<slug>", methods=["GET"])
def view_form(slug):
    db = current_app.mongo_db
    forms = db["forms"]
    subs = db["submissions"]

    form = forms.find_one({"slug": slug})
    if not form: abort(404, "Form not found")

    q = (request.args.get("q") or "").strip()
    page = _int(request.args.get("page"), 1)
    per_page = min(max(_int(request.args.get("per_page"), 20), 10), 200)
    base = {"slug": slug}

    if q:
        cursor = subs.find(base).sort("created_at", -1)
        all_items = list(cursor)
        q_low = q.lower()
        filtered = []
        for it in all_items:
            blob = " ".join([str(v) for v in (it.get("fields") or {}).values()]).lower()
            if q_low in blob:
                filtered.append(it)
        total = len(filtered)
        items = filtered[(page-1)*per_page: page*per_page]
    else:
        total = subs.count_documents(base)
        items = list(subs.find(base).sort("created_at", -1).skip((page-1)*per_page).limit(per_page))

    for it in items:
        it["_id"] = str(it["_id"])
        it["created_at_str"] = _format_dt(it.get("created_at"))

    pages = math.ceil(total / per_page) if per_page else 1
    columns = [{"id": f["id"], "label": f.get("label", f["id"])} for f in form.get("fields", [])]
    share_url = url_for("form_bp.render_form", slug=form["slug"], _external=True)

    return render_template(
        "admin_form_detail.html",
        form=form,
        submissions=items,
        columns=columns,
        q=q, page=page, per_page=per_page, pages=pages, total=total,
        share_url=share_url
    )

# ---------- Update a submission ----------
@admin_bp.route("/forms/<slug>/submissions/<sub_id>", methods=["PATCH", "PUT"])
def update_submission(slug, sub_id):
    db = current_app.mongo_db
    forms = db["forms"]
    subs = db["submissions"]

    form = forms.find_one({"slug": slug})
    if not form:
        return jsonify({"ok": False, "error": "Form not found"}), 404
    try:
        oid = ObjectId(sub_id)
    except Exception:
        return jsonify({"ok": False, "error": "Invalid submission id"}), 400

    sub = subs.find_one({"_id": oid, "slug": slug})
    if not sub:
        return jsonify({"ok": False, "error": "Submission not found"}), 404

    data = request.get_json(force=True) or {}
    new_fields = data.get("fields") or {}
    if not isinstance(new_fields, dict):
        return jsonify({"ok": False, "error": "Invalid payload"}), 400

    merged = dict(sub.get("fields") or {})
    for fid, val in new_fields.items():
        merged[fid] = "" if val is None else str(val)

    subs.update_one({"_id": oid}, {"$set": {"fields": merged}})
    return jsonify({"ok": True})

# ---------- Delete a submission ----------
@admin_bp.route("/forms/<slug>/submissions/<sub_id>", methods=["DELETE"])
def delete_submission(slug, sub_id):
    db = current_app.mongo_db
    subs = db["submissions"]
    try:
        oid = ObjectId(sub_id)
    except Exception:
        return jsonify({"ok": False, "error": "Invalid submission id"}), 400
    res = subs.delete_one({"_id": oid, "slug": slug})
    if res.deleted_count == 0:
        return jsonify({"ok": False, "error": "Submission not found"}), 404
    return jsonify({"ok": True})

# ---------- NEW: Suspend / Activate a form ----------
@admin_bp.route("/forms/<slug>/suspend", methods=["PATCH"])
def suspend_form(slug):
    """
    Body: {"suspended": true|false}
    When suspended=true, public /f/<slug> should be considered disabled by your renderer.
    """
    db = current_app.mongo_db
    forms = db["forms"]

    form = forms.find_one({"slug": slug})
    if not form:
        return jsonify({"ok": False, "error": "Form not found"}), 404

    data = request.get_json(force=True) or {}
    suspended = bool(data.get("suspended", True))
    forms.update_one({"_id": form["_id"]}, {"$set": {"suspended": suspended, "updated_at": datetime.utcnow()}})
    return jsonify({"ok": True, "suspended": suspended})

# ---------- NEW: Delete a form (and its submissions) ----------
@admin_bp.route("/forms/<slug>", methods=["DELETE"])
def delete_form(slug):
    db = current_app.mongo_db
    forms = db["forms"]
    subs = db["submissions"]

    form = forms.find_one({"slug": slug})
    if not form:
        return jsonify({"ok": False, "error": "Form not found"}), 404

    # delete submissions first
    subs.delete_many({"slug": slug})
    # delete the form
    forms.delete_one({"_id": form["_id"]})

    return jsonify({"ok": True})

@admin_bp.route("/forms/<slug>/export/<fmt>", methods=["GET"])
def export_form(slug, fmt):
    """
    Excel export excludes 'Submitted At'; PDF includes it.
    Both now prepend a 'No' column that numbers the rows starting at 1.
    """
    db = current_app.mongo_db
    forms = db["forms"]
    subs = db["submissions"]

    form = forms.find_one({"slug": slug})
    if not form: abort(404, "Form not found")

    submissions = list(subs.find({"slug": slug}).sort("created_at", -1))

    if fmt.lower() in ("xlsx", "excel"):
        if pd is None:
            abort(400, "Excel export requires 'pandas' and 'openpyxl' (pip install pandas openpyxl)")
        df = _build_dataframe_without_submitted_at(form, submissions, include_no=True)
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Submissions")
        output.seek(0)
        return send_file(
            output, as_attachment=True,
            download_name=f"{form['slug']}-submissions.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

    if fmt.lower() == "pdf":
        try:
            styles = getSampleStyleSheet()
        except Exception:
            abort(400, "PDF export requires 'reportlab' (pip install reportlab)")
        # headers: No + Submitted At + field labels
        headers = ["No", "Submitted At"] + [f.get("label", f["id"]) for f in form.get("fields", [])]
        data = [headers]
        for idx, s in enumerate(submissions, start=1):
            row = [str(idx), _format_dt(s.get("created_at"))]
            for f in form.get("fields", []):
                row.append(s.get("fields", {}).get(f["id"], "")) 
            data.append(row)

        output = io.BytesIO()
        doc = SimpleDocTemplate(output, pagesize=landscape(A4),
                                leftMargin=24, rightMargin=24, topMargin=24, bottomMargin=24)
        story = []
        story.append(Paragraph(f"{form['title']} — Submissions", styles["Title"]))
        story.append(Paragraph(f"Exported: {datetime.utcnow():%Y-%m-%d %H:%M UTC}", styles["Normal"]))
        story.append(Spacer(1, 12))
        t = Table(data, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#0ea5e9")),
            ("TEXTCOLOR", (0,0), (-1,0), colors.white),
            ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
            ("FONTSIZE", (0,0), (-1,0), 10),
            ("GRID", (0,0), (-1,-1), 0.25, colors.HexColor("#e5e7eb")),
            ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, colors.HexColor("#f9fafb")]),
            ("VALIGN", (0,0), (-1,-1), "TOP"),
        ]))
        story.append(t)
        doc.build(story)
        output.seek(0)
        return send_file(
            output, as_attachment=True,
            download_name=f"{form['slug']}-submissions.pdf",
            mimetype="application/pdf"
        )

    abort(400, "Unsupported export format. Use xlsx or pdf.")
