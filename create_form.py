from flask import (
    Blueprint, render_template, request, jsonify, current_app,
    send_file, url_for, Response
)
from werkzeug.utils import secure_filename
from datetime import datetime
from bson import ObjectId
from gridfs import GridFS
import io, re, uuid, csv, itertools

form_bp = Blueprint("form_bp", __name__)

# Added "select" for options-based fields
ALLOWED_FIELD_TYPES = ["text", "number", "email", "tel", "date", "textarea", "select"]

MAX_TITLE_LEN = 120
MAX_DESC_LEN = 300
MAX_FIELDS = 100
MAX_LABEL_LEN = 80
MAX_PLACEHOLDER_LEN = 120
MAX_FORMAT_LEN = 64

# Limits for options (for "select")
MAX_OPTION_COUNT = 100
MAX_OPTION_LEN = 80

DEFAULT_THEME = {
    "key": "sea",
    "name": "Sea Blue",
    "brand": "#0ea5e9",
    "ok": "#16a34a",
    "ink": "#0b1320",
    "muted": "#64748b",
    "ring": "#e5e7eb",
    "soft": "#f6f8fb",
}

HEX_RE = re.compile(r"^#([0-9a-fA-F]{6})$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TEL_RE = re.compile(r"^[0-9+()\-.\s]{3,}$")  # permissive; UI can be stricter

def slugify(title: str, col):
    base = re.sub(r"[^a-zA-Z0-9]+", "-", (title or "form")).strip("-").lower() or "form"
    if col.find_one({"slug": base}):
        base = f"{base}-{uuid.uuid4().hex[:8]}"
    return base

def format_to_regex(fmt: str) -> str:
    if not fmt:
        return ""
    out, i, n = [], 0, len(fmt)
    while i < n:
        ch = fmt[i]; j = i
        while j < n and fmt[j] == ch:
            j += 1
        run_len = j - i
        if ch == "X":
            piece = r"\d" + (f"{{{run_len}}}" if run_len > 1 else "")
        elif ch == "A":
            piece = r"[A-Za-z]" + (f"{{{run_len}}}" if run_len > 1 else "")
        elif ch == "*":
            piece = r".+"  # greedy by design
        else:
            piece = re.escape(ch * run_len)
        out.append(piece)
        i = j
    return "^" + "".join(out) + "$"

def _sanitize_hex(val: str, fallback: str) -> str:
    if isinstance(val, str) and HEX_RE.match(val):
        return val
    return fallback

def _sanitize_theme(theme_in: dict | None) -> dict:
    if not isinstance(theme_in, dict):
        return DEFAULT_THEME.copy()
    out = {
        "key": str(theme_in.get("key") or DEFAULT_THEME["key"])[:40],
        "name": str(theme_in.get("name") or DEFAULT_THEME["name"])[:60],
        "brand": _sanitize_hex(theme_in.get("brand"), DEFAULT_THEME["brand"]),
        "ok": _sanitize_hex(theme_in.get("ok"), DEFAULT_THEME["ok"]),
        "ink": _sanitize_hex(theme_in.get("ink"), DEFAULT_THEME["ink"]),
        "muted": _sanitize_hex(theme_in.get("muted"), DEFAULT_THEME["muted"]),
        "ring": _sanitize_hex(theme_in.get("ring"), DEFAULT_THEME["ring"]),
        "soft": _sanitize_hex(theme_in.get("soft"), DEFAULT_THEME["soft"]),
    }
    return out

def _coerce_options(raw) -> list[str]:
    """
    Accepts list[str] or a newline/comma-separated string from client,
    returns unique, trimmed, non-empty options (length-limited).
    """
    opts = []
    if isinstance(raw, list):
        opts = [str(x) for x in raw]
    elif isinstance(raw, str):
        # split on newlines or commas
        parts = re.split(r"[\n,]", raw)
        opts = [p for p in (s.strip() for s in parts)]
    else:
        return []

    # Clean: non-empty, unique (preserve order), length cap
    seen = set()
    cleaned = []
    for o in opts:
        if not o:
            continue
        o = o[:MAX_OPTION_LEN]
        if o.lower() in seen:
            continue
        seen.add(o.lower())
        cleaned.append(o)
        if len(cleaned) >= MAX_OPTION_COUNT:
            break
    return cleaned

def _sanitize_fields(fields_in):
    if not isinstance(fields_in, list):
        return None, "Invalid fields payload."
    if len(fields_in) > MAX_FIELDS:
        return None, f"Too many fields (max {MAX_FIELDS})."

    fields = []
    for f in fields_in:
        if not isinstance(f, dict):
            continue
        ftype = (f.get("type") or "").strip()
        if ftype not in ALLOWED_FIELD_TYPES:
            continue

        label_clean = (f.get("label") or ftype.title()).strip()[:MAX_LABEL_LEN]
        fid_source = (f.get("label") or ftype).strip().lower()
        fid = re.sub(r"\s+", "_", fid_source)
        fid = re.sub(r"[^a-z0-9_]+", "", fid)

        cf = {
            "id": fid or ftype,
            "label": label_clean or ftype.title(),
            "type": ftype,
            "required": bool(f.get("required")),
        }

        placeholder = (f.get("placeholder") or "").strip()[:MAX_PLACEHOLDER_LEN]
        if placeholder and ftype != "select":  # placeholder not shown for select
            cf["placeholder"] = placeholder

        fmt = (f.get("format") or "").strip()[:MAX_FORMAT_LEN]
        if fmt and ftype not in ("select", "date", "textarea"):
            cf["format"] = fmt
            cf["pattern"] = format_to_regex(fmt)

        # NEW: options for "select"
        if ftype == "select":
            options_raw = f.get("options") if "options" in f else f.get("options_text", "")
            options = _coerce_options(options_raw)
            if not options:
                return None, f"Field '{label_clean}': add at least one option."
            cf["options"] = options
            # Optional default
            default_val = (f.get("default") or "").strip()
            if default_val and default_val in options:
                cf["default"] = default_val

        fields.append(cf)

    if not fields:
        return None, "Add at least one field."
    return fields, None

# ===== Helpers for submissions ==============================================

def _get_form_and_collections(slug: str):
    db = current_app.mongo_db
    forms_col = db["forms"]
    subs_col = db["submissions"]
    form = forms_col.find_one({"slug": slug})
    return db, forms_col, subs_col, form

def _field_order(form) -> list[str]:
    return [f.get("id") for f in (form.get("fields") or []) if f.get("id")]

def _field_map(form) -> dict:
    return {f.get("id"): f for f in (form.get("fields") or []) if f.get("id")}

def _validate_field_value(field_def: dict, value: str) -> tuple[bool, str | None]:
    if value is None:
        value = ""
    value = str(value)

    # required
    if field_def.get("required") and value.strip() == "":
        return False, f"Missing required field: {field_def.get('label') or field_def.get('id')}"

    # For empty non-required, skip other checks
    if not field_def.get("required") and value.strip() == "":
        return True, None

    # type-specific checks
    ftype = field_def.get("type")
    if ftype == "number" and value.strip():
        try:
            float(value)
        except ValueError:
            return False, f"{field_def.get('label') or field_def.get('id')} must be a number."
    if ftype == "email" and value.strip():
        if not EMAIL_RE.match(value.strip()):
            return False, f"{field_def.get('label') or field_def.get('id')} must be a valid email."
    if ftype == "tel" and value.strip():
        if not TEL_RE.match(value.strip()):
            return False, f"{field_def.get('label') or field_def.get('id')} must be a valid phone."
    if ftype == "select":
        options = field_def.get("options") or []
        if options and value not in options:
            return False, f"{field_def.get('label') or field_def.get('id')} must be one of the provided options."

    # mask
    patt = field_def.get("pattern")
    if patt and value.strip():
        try:
            if not re.fullmatch(patt, value):
                return False, f"{field_def.get('label') or field_def.get('id')} is not in the required format."
        except re.error:
            pass
    return True, None

# ===== Routes ================================================================

@form_bp.route("/builder", methods=["GET"])
def builder():
    edit_slug = request.args.get("edit") or None
    return render_template("create_form.html", edit_slug=edit_slug)

@form_bp.route("/builder/<slug>", methods=["GET"])
def builder_edit(slug):
    return render_template("create_form.html", edit_slug=slug)

@form_bp.route("/api/upload", methods=["POST"])
def upload_form_image():
    db = current_app.mongo_db
    fs = GridFS(db)
    file = request.files.get("image")
    if not file:
        return jsonify({"ok": False, "error": "No file provided."}), 400
    filename = secure_filename(file.filename or "image")
    content_type = file.mimetype or "application/octet-stream"
    _id = fs.put(file.stream, filename=filename, content_type=content_type)
    return jsonify({"ok": True, "file_id": str(_id)})

@form_bp.route("/api/forms", methods=["POST"])
def save_form():
    db = current_app.mongo_db
    forms_col = db["forms"]
    data = request.get_json(force=True) or {}

    title = (data.get("title") or "").strip()[:MAX_TITLE_LEN]
    if not title:
        return jsonify({"ok": False, "error": "Form title is required."}), 400

    description = ((data.get("description") or "").strip()[:MAX_DESC_LEN]) or None
    theme = _sanitize_theme(data.get("theme"))
    form_image_id = data.get("form_image_id") or None
    if form_image_id:
        try:
            _ = ObjectId(form_image_id)
        except Exception:
            form_image_id = None

    fields, err = _sanitize_fields(data.get("fields") or [])
    if err:
        return jsonify({"ok": False, "error": err}), 400

    slug = slugify(title, forms_col)
    now = datetime.utcnow()
    doc = {
        "title": title,
        "description": description,
        "theme": theme,
        "form_image_id": form_image_id,
        "fields": fields,
        "slug": slug,
        "created_at": now,
        "updated_at": now,
        "suspended": False,  # keep your flag
    }
    forms_col.insert_one(doc)
    return jsonify({"ok": True, "slug": slug, "view_url": f"/f/{slug}"}), 201

@form_bp.route("/api/forms/<slug>", methods=["GET"])
def get_form(slug):
    db = current_app.mongo_db
    forms_col = db["forms"]
    form = forms_col.find_one({"slug": slug})
    if not form:
        return jsonify({"ok": False, "error": "Form not found"}), 404
    form["_id"] = str(form["_id"])
    if form.get("form_image_id"):
        form["form_image_url"] = url_for(".get_file", file_id=form["form_image_id"])
    if form.get("created_at"):
        form["created_at_str"] = form["created_at"].strftime("%Y-%m-%d %H:%M")
    if form.get("updated_at"):
        form["updated_at_str"] = form["updated_at"].strftime("%Y-%m-%d %H:%M")
    form["suspended"] = bool(form.get("suspended", False))
    return jsonify({"ok": True, "form": form})

@form_bp.route("/api/forms/<slug>", methods=["PUT", "PATCH"])
def update_form(slug):
    db = current_app.mongo_db
    forms_col = db["forms"]
    form = forms_col.find_one({"slug": slug})
    if not form:
        return jsonify({"ok": False, "error": "Form not found"}), 404

    data = request.get_json(force=True) or {}

    title = data.get("title")
    if title is not None:
        title = str(title).strip()[:MAX_TITLE_LEN]
        if not title:
            return jsonify({"ok": False, "error": "Form title cannot be empty."}), 400

    description = data.get("description")
    if description is not None:
        description = str(description).strip()[:MAX_DESC_LEN] or None

    theme = data.get("theme")
    if theme is not None:
        theme = _sanitize_theme(theme)

    form_image_id = data.get("form_image_id")
    if form_image_id is not None:
        if form_image_id:
            try:
                _ = ObjectId(form_image_id)
            except Exception:
                form_image_id = None

    fields_in = data.get("fields")
    if fields_in is not None:
        fields, err = _sanitize_fields(fields_in)
        if err:
            return jsonify({"ok": False, "error": err}), 400
    else:
        fields = None

    suspended_in = data.get("suspended")
    suspended_val = None
    if suspended_in is not None:
        suspended_val = bool(suspended_in)

    update_doc = {"updated_at": datetime.utcnow()}
    if title is not None: update_doc["title"] = title
    if description is not None: update_doc["description"] = description
    if theme is not None: update_doc["theme"] = theme
    if form_image_id is not None: update_doc["form_image_id"] = form_image_id
    if fields is not None: update_doc["fields"] = fields
    if suspended_val is not None: update_doc["suspended"] = suspended_val

    forms_col.update_one({"_id": form["_id"]}, {"$set": update_doc})
    return jsonify({"ok": True, "slug": slug, "view_url": f"/f/{slug}"}), 200

@form_bp.route("/f/<slug>", methods=["GET"])
def render_form(slug):
    db = current_app.mongo_db
    form = db["forms"].find_one({"slug": slug})
    if not form:
        return "Form not found", 404
    if form.get("suspended"):
        return render_template("runtime_form.html", form=form, suspended=True), 403
    return render_template("runtime_form.html", form=form, suspended=False)

@form_bp.route("/f/<slug>/submit", methods=["POST"])
def submit_form(slug):
    db = current_app.mongo_db
    forms_col = db["forms"]
    submissions_col = db["submissions"]

    form = forms_col.find_one({"slug": slug})
    if not form:
        return "Form not found", 404

    if form.get("suspended"):
        return render_template("runtime_form.html", form=form, suspended=True), 403

    payload = {
        "form_id": form["_id"],
        "slug": slug,
        "created_at": datetime.utcnow(),
        "fields": {},
    }

    fmap = _field_map(form)
    for f in (form.get("fields") or []):
        fid = f.get("id")
        if not fid:
            continue
        value = request.form.get(fid, "")
        # If select and empty but default exists, apply default
        if (f.get("type") == "select") and (not value.strip()):
            d = f.get("default")
            if d:
                value = d
        ok, err = _validate_field_value(f, value)
        if not ok:
            return err, 400
        payload["fields"][fid] = value

    submissions_col.insert_one(payload)
    return render_template("submitted.html", form=form)

@form_bp.route("/file/<file_id>", methods=["GET"])
def get_file(file_id):
    db = current_app.mongo_db
    fs = GridFS(db)
    try:
        gridout = fs.get(ObjectId(file_id))
    except Exception:
        return "File not found", 404
    return send_file(
        io.BytesIO(gridout.read()),
        mimetype=gridout.content_type or "application/octet-stream",
        download_name=gridout.filename or "file"
    )

# ====== Submissions API (List / Update / Delete / Export) ====================

@form_bp.route("/api/forms/<slug>/submissions", methods=["GET"])
def list_submissions(slug):
    """
    Returns submissions for a form as JSON:
    {
      ok: true,
      columns: [field ids ordered],
      rows: [{_id, ...flattened fields..., created_at_str}, ...],
      page, per_page, total
    }
    """
    db, forms_col, subs_col, form = _get_form_and_collections(slug)
    if not form:
        return jsonify({"ok": False, "error": "Form not found"}), 404

    page = max(int(request.args.get("page", 1) or 1), 1)
    per_page = min(max(int(request.args.get("per_page", 20) or 20), 1), 200)

    q = {"slug": slug}
    total = subs_col.count_documents(q)
    cursor = subs_col.find(q).sort("created_at", -1).skip((page-1)*per_page).limit(per_page)

    cols = _field_order(form)
    rows = []
    for doc in cursor:
        row = {"_id": str(doc.get("_id"))}
        fields = doc.get("fields", {})
        for c in cols:
            row[c] = fields.get(c, "")
        row["created_at_str"] = doc.get("created_at").strftime("%Y-%m-%d %H:%M") if doc.get("created_at") else ""
        rows.append(row)

    return jsonify({
        "ok": True,
        "columns": cols,
        "rows": rows,
        "page": page,
        "per_page": per_page,
        "total": total
    })

@form_bp.route("/api/forms/<slug>/submissions/<sub_id>", methods=["PATCH", "PUT"])
def update_submission(slug, sub_id):
    """
    Update one submission's fields. Body:
    { "fields": { "<field_id>": "new value", ... } }
    Validates per field type/mask; ignores unknown field ids.
    """
    db, forms_col, subs_col, form = _get_form_and_collections(slug)
    if not form:
        return jsonify({"ok": False, "error": "Form not found"}), 404
    try:
        oid = ObjectId(sub_id)
    except Exception:
        return jsonify({"ok": False, "error": "Invalid submission id"}), 400

    sub = subs_col.find_one({"_id": oid, "slug": slug})
    if not sub:
        return jsonify({"ok": False, "error": "Submission not found"}), 404

    data = request.get_json(force=True) or {}
    new_fields = data.get("fields") or {}
    if not isinstance(new_fields, dict):
        return jsonify({"ok": False, "error": "Invalid payload"}), 400

    fmap = _field_map(form)
    merged = dict(sub.get("fields") or {})
    for fid, val in new_fields.items():
        if fid not in fmap:
            continue
        ok, err = _validate_field_value(fmap[fid], str(val) if val is not None else "")
        if not ok:
            return jsonify({"ok": False, "error": err}), 400
        merged[fid] = str(val) if val is not None else ""

    subs_col.update_one({"_id": oid}, {"$set": {"fields": merged}})
    return jsonify({"ok": True})

@form_bp.route("/api/forms/<slug>/submissions/<sub_id>", methods=["DELETE"])
def delete_submission(slug, sub_id):
    db, forms_col, subs_col, form = _get_form_and_collections(slug)
    if not form:
        return jsonify({"ok": False, "error": "Form not found"}), 404
    try:
        oid = ObjectId(sub_id)
    except Exception:
        return jsonify({"ok": False, "error": "Invalid submission id"}), 400
    res = subs_col.delete_one({"_id": oid, "slug": slug})
    if res.deleted_count == 0:
        return jsonify({"ok": False, "error": "Submission not found"}), 404
    return jsonify({"ok": True})

@form_bp.route("/api/forms/<slug>/submissions/export", methods=["GET"])
def export_submissions(slug):
    """
    Export *only fields* to a CSV (Excel friendly).
    Explicitly omits created_at/date-submitted.
    Query params: ?columns=f1,f2,f3 (optional) to limit/reorder.
    """
    db, forms_col, subs_col, form = _get_form_and_collections(slug)
    if not form:
        return jsonify({"ok": False, "error": "Form not found"}), 404

    form_cols = _field_order(form)
    req_cols = request.args.get("columns")
    if req_cols:
        wanted = [c for c in (rc.strip() for rc in req_cols.split(",")) if c]
        columns = [c for c in wanted if c in form_cols] or form_cols[:]
    else:
        columns = form_cols[:]

    def generate():
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(columns)
        yield output.getvalue()
        output.seek(0); output.truncate(0)

        for doc in subs_col.find({"slug": slug}).sort("created_at", -1):
            row = [doc.get("fields", {}).get(c, "") for c in columns]
            writer.writerow(row)
            yield output.getvalue()
            output.seek(0); output.truncate(0)

    filename = f"{slug}-submissions.csv"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Type": "text/csv; charset=utf-8"
    }
    return Response(generate(), headers=headers)
