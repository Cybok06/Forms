from flask import (
    Blueprint, render_template, request, jsonify, current_app,
    send_file, url_for, Response, redirect, session
)
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta
from bson import ObjectId
from gridfs import GridFS
import io, re, uuid, csv
from urllib.parse import urlparse
from image_storage import ImageUploadError, delete_image, upload_image

form_bp = Blueprint("form_bp", __name__)

# Added "select" for options-based fields
ALLOWED_FIELD_TYPES = ["text", "number", "email", "tel", "date", "textarea", "select", "image"]

MAX_TITLE_LEN = 120
MAX_DESC_LEN = 300
MAX_FIELDS = 100
MAX_LABEL_LEN = 80
MAX_PLACEHOLDER_LEN = 120
MAX_FORMAT_LEN = 64
MAX_AD_URL_LEN = 500

# Limits for options (for "select")
MAX_OPTION_COUNT = 100
MAX_OPTION_LEN = 80

DEFAULT_THEME = {
    "key": "royal",
    "name": "Royal Blue",
    "brand": "#356BEA",
    "ok": "#16a34a",
    "ink": "#0D1B46",
    "muted": "#8A94A6",
    "ring": "#E5EAF5",
    "soft": "#EAF1FF",
}

HEX_RE = re.compile(r"^#([0-9a-fA-F]{6})$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TEL_RE = re.compile(r"^[0-9+()\-.\s]{3,}$")  # permissive; UI can be stricter

# ====== PRG + double-submit guard settings ======
NONCE_SESSION_PREFIX = "form_nonce_used:"
NONCE_SESSION_LIMIT = 250  # soft cap per browser session (avoid session bloat)


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


def _sanitize_object_id(value) -> str | None:
    if not value:
        return None
    value = str(value)
    try:
        _ = ObjectId(value)
    except Exception:
        return None
    return value


def _sanitize_redirect_url(value) -> str | None:
    value = (str(value or "").strip())[:MAX_AD_URL_LEN]
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return value


def _sanitize_ad(ad_in: dict | None) -> tuple[dict, str | None]:
    if not isinstance(ad_in, dict):
        return {"enabled": False, "image_id": None, "redirect_url": None}, None

    enabled = bool(ad_in.get("enabled"))
    image_id = _sanitize_object_id(ad_in.get("image_id"))
    raw_redirect_url = str(ad_in.get("redirect_url") or "").strip()
    redirect_url = _sanitize_redirect_url(raw_redirect_url)

    if enabled and not image_id:
        return {"enabled": False, "image_id": None, "redirect_url": None}, "Upload an ad image or turn off ads."
    if enabled and raw_redirect_url and not redirect_url:
        return {"enabled": False, "image_id": None, "redirect_url": None}, "Enter a valid ad redirect link starting with http:// or https://."

    return {
        "enabled": enabled,
        "image_id": image_id if enabled else None,
        "redirect_url": redirect_url if enabled else None,
    }, None


def _coerce_options(raw) -> list[str]:
    """
    Accepts list[str] or a newline/comma-separated string from client,
    returns unique, trimmed, non-empty options (length-limited).
    """
    opts = []
    if isinstance(raw, list):
        opts = [str(x) for x in raw]
    elif isinstance(raw, str):
        parts = re.split(r"[\n,]", raw)
        opts = [p for p in (s.strip() for s in parts)]
    else:
        return []

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
    used_ids = set()
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

        base_fid = fid or ftype
        fid = base_fid
        counter = 2
        while fid in used_ids:
            fid = f"{base_fid}_{counter}"
            counter += 1
        used_ids.add(fid)

        cf = {
            "id": fid,
            "label": label_clean or ftype.title(),
            "type": ftype,
            "required": bool(f.get("required")),
        }

        placeholder = (f.get("placeholder") or "").strip()[:MAX_PLACEHOLDER_LEN]
        if placeholder and ftype not in ("select", "image"):
            cf["placeholder"] = placeholder

        fmt = (f.get("format") or "").strip()[:MAX_FORMAT_LEN]
        if fmt and ftype not in ("select", "date", "textarea", "image"):
            cf["format"] = fmt
            cf["pattern"] = format_to_regex(fmt)

        if ftype == "select":
            options_raw = f.get("options") if "options" in f else f.get("options_text", "")
            options = _coerce_options(options_raw)
            if not options:
                return None, f"Field '{label_clean}': add at least one option."
            cf["options"] = options
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


def _export_value(value):
    if isinstance(value, dict):
        return value.get("original_name") or "Image"
    return "" if value is None else value


def _validate_field_value(field_def: dict, value: str) -> tuple[bool, str | None]:
    if field_def.get("type") == "image":
        has_image = isinstance(value, dict) and bool(value.get("url") or value.get("file_id"))
        if field_def.get("required") and not has_image:
            return False, f"Missing required field: {field_def.get('label') or field_def.get('id')}"
        return True, None
    if value is None:
        value = ""
    value = str(value)

    if field_def.get("required") and value.strip() == "":
        return False, f"Missing required field: {field_def.get('label') or field_def.get('id')}"

    if not field_def.get("required") and value.strip() == "":
        return True, None

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

    patt = field_def.get("pattern")
    if patt and value.strip():
        try:
            if not re.fullmatch(patt, value):
                return False, f"{field_def.get('label') or field_def.get('id')} is not in the required format."
        except re.error:
            pass
    return True, None


def _session_used_nonces_key(slug: str) -> str:
    return f"{NONCE_SESSION_PREFIX}{slug}"


def _is_nonce_used(slug: str, nonce: str) -> bool:
    if not nonce:
        return False
    used = session.get(_session_used_nonces_key(slug), {})
    return bool(used.get(nonce))


def _mark_nonce_used(slug: str, nonce: str) -> None:
    if not nonce:
        return
    key = _session_used_nonces_key(slug)
    used = session.get(key, {})
    if not isinstance(used, dict):
        used = {}
    used[nonce] = True

    # soft cap to avoid huge session
    if len(used) > NONCE_SESSION_LIMIT:
        # keep last ~100 items (dict insertion order preserved in py3.7+)
        trimmed = {}
        for k in list(used.keys())[-100:]:
            trimmed[k] = True
        used = trimmed

    session[key] = used


def _cleanup_expired_image_uploads(db) -> None:
    uploads = db["temporary_image_uploads"]
    expired = list(uploads.find({"expires_at": {"$lte": datetime.utcnow()}}).limit(100))
    for item in expired:
        delete_image(_pending_image_meta(item))
        uploads.delete_one({"_id": item["_id"]})


def _pending_image_meta(item: dict | None) -> dict | None:
    if not isinstance(item, dict):
        return None
    if isinstance(item.get("image"), dict):
        return item["image"]
    if item.get("storage") or item.get("image_id") or item.get("url"):
        return {
            "storage": item.get("storage"),
            "image_id": item.get("image_id"),
            "url": item.get("url"),
            "original_name": item.get("original_name"),
            "content_type": item.get("content_type"),
        }
    return None


def _submission_image_meta(image: dict | None) -> dict | None:
    if not isinstance(image, dict):
        return None
    meta = {
        "storage": image.get("storage"),
        "url": image.get("url"),
        "original_name": image.get("original_name"),
        "content_type": image.get("content_type"),
    }
    if image.get("storage") == "cloudflare":
        meta["image_id"] = image.get("image_id")
    elif image.get("storage") == "cloudinary":
        meta["public_id"] = image.get("public_id")
    elif image.get("storage") == "gridfs":
        meta["file_id"] = image.get("file_id")
    return meta


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
    form_image_id = _sanitize_object_id(data.get("form_image_id"))
    ad, ad_err = _sanitize_ad(data.get("ad"))
    if ad_err:
        return jsonify({"ok": False, "error": ad_err}), 400

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
        "ad": ad,
        "fields": fields,
        "slug": slug,
        "created_at": now,
        "updated_at": now,
        "suspended": False,
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
    ad = form.get("ad") if isinstance(form.get("ad"), dict) else {}
    if ad.get("image_id"):
        ad["image_url"] = url_for(".get_file", file_id=ad["image_id"])
    form["ad"] = {
        "enabled": bool(ad.get("enabled")),
        "image_id": ad.get("image_id") or None,
        "redirect_url": ad.get("redirect_url") or None,
        "image_url": ad.get("image_url") or None,
    }
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
        form_image_id = _sanitize_object_id(form_image_id)

    ad = None
    if "ad" in data:
        ad, ad_err = _sanitize_ad(data.get("ad"))
        if ad_err:
            return jsonify({"ok": False, "error": ad_err}), 400

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
    if ad is not None: update_doc["ad"] = ad
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

    # ✅ new: generate a nonce so refresh cannot re-post the same submission
    nonce = uuid.uuid4().hex
    return render_template("runtime_form.html", form=form, suspended=False, nonce=nonce)


@form_bp.route("/f/<slug>/upload-image/<field_id>", methods=["POST"])
def upload_submission_image(slug, field_id):
    db = current_app.mongo_db
    form = db["forms"].find_one({"slug": slug})
    if not form:
        return jsonify({"ok": False, "error": "Form not found."}), 404
    if form.get("suspended"):
        return jsonify({"ok": False, "error": "This form is unavailable."}), 403

    field = _field_map(form).get(field_id)
    if not field or field.get("type") != "image":
        return jsonify({"ok": False, "error": "Invalid image field."}), 400

    file_obj = request.files.get("image")
    try:
        image = upload_image(file_obj, folder=f"form_submissions/{slug}")
    except ImageUploadError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception:
        current_app.logger.exception("Immediate image upload failed for form %s", slug)
        return jsonify({"ok": False, "error": "The image service could not complete the upload."}), 502

    token = uuid.uuid4().hex
    uploads = db["temporary_image_uploads"]
    try:
        _cleanup_expired_image_uploads(db)
        uploads.insert_one({
            "token": token,
            "slug": slug,
            "field_id": field_id,
            "storage": image.get("storage"),
            "image_id": image.get("image_id"),
            "url": image.get("url"),
            "original_name": image.get("original_name"),
            "content_type": image.get("content_type"),
            "image": image,
            "created_at": datetime.utcnow(),
            "expires_at": datetime.utcnow() + timedelta(hours=24),
            "claimed": False,
        })
    except Exception:
        delete_image(image)
        raise

    previous_token = (request.form.get("replace_token") or "").strip()
    if previous_token:
        previous = uploads.find_one_and_delete({
            "token": previous_token,
            "slug": slug,
            "field_id": field_id,
            "claimed": {"$ne": True},
        })
        if previous:
            delete_image(_pending_image_meta(previous))

    return jsonify({
        "ok": True,
        "token": token,
        "filename": image.get("original_name") or "Image",
        "image_id": image.get("image_id") or "",
        "url": image.get("url") or "",
        "content_type": image.get("content_type") or "",
    })


@form_bp.route("/f/<slug>/uploaded-image/<token>", methods=["DELETE"])
def discard_submission_image(slug, token):
    uploads = current_app.mongo_db["temporary_image_uploads"]
    item = uploads.find_one_and_delete({
        "token": token,
        "slug": slug,
        "claimed": {"$ne": True},
    })
    if item:
        delete_image(_pending_image_meta(item))
    return jsonify({"ok": True})


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

    # ✅ NEW: duplicate-submit guard
    nonce = (request.form.get("_nonce") or "").strip()
    if nonce and _is_nonce_used(slug, nonce):
        # Already processed → redirect to submitted page (GET)
        return redirect(url_for("form_bp.submitted", slug=slug), code=303)

    payload = {
        "form_id": form["_id"],
        "slug": slug,
        "created_at": datetime.utcnow(),
        "fields": {},
    }

    # Validate all ordinary values before uploading any files.
    for f in (form.get("fields") or []):
        fid = f.get("id")
        if not fid:
            continue
        if f.get("type") == "image":
            continue

        value = request.form.get(fid, "")
        if (f.get("type") == "select") and (not value.strip()):
            d = f.get("default")
            if d:
                value = d
        ok, err = _validate_field_value(f, value)
        if not ok:
            return err, 400
        payload["fields"][fid] = value

    uploaded_images = []
    claimed_tokens = []

    def rollback_images():
        for uploaded in uploaded_images:
            delete_image(uploaded)
        if claimed_tokens:
            db["temporary_image_uploads"].update_many(
                {"token": {"$in": claimed_tokens}},
                {"$set": {"claimed": False}},
            )

    for f in (form.get("fields") or []):
        if f.get("type") != "image" or not f.get("id"):
            continue
        token = (request.form.get(f"_image_token_{f['id']}") or "").strip()
        uploaded_file = request.files.get(f["id"])
        value = None
        if token:
            uploads = db["temporary_image_uploads"]
            staged = uploads.find_one({
                "token": token,
                "slug": slug,
                "field_id": f["id"],
                "expires_at": {"$gt": datetime.utcnow()},
                "claimed": {"$ne": True},
            })
            if staged:
                claimed = uploads.update_one(
                    {"_id": staged["_id"], "claimed": {"$ne": True}},
                    {"$set": {"claimed": True}},
                )
                if claimed.modified_count:
                    value = _submission_image_meta(_pending_image_meta(staged))
                    claimed_tokens.append(token)
        elif uploaded_file and uploaded_file.filename:
            try:
                value = upload_image(uploaded_file, folder=f"form_submissions/{slug}")
                value = _submission_image_meta(value)
                uploaded_images.append(value)
            except ImageUploadError as exc:
                rollback_images()
                return str(exc), 400
            except Exception:
                rollback_images()
                current_app.logger.exception("Image upload failed for form %s", slug)
                return "The image service could not complete the upload. Please try again.", 502
        elif f.get("required"):
            rollback_images()
            return f"Please upload an image for {f.get('label') or f.get('id')}.", 400
        ok, err = _validate_field_value(f, value)
        if not ok:
            rollback_images()
            return err, 400
        payload["fields"][f["id"]] = value or ""

    try:
        res = submissions_col.insert_one(payload)
    except Exception:
        rollback_images()
        raise

    if claimed_tokens:
        db["temporary_image_uploads"].delete_many({"token": {"$in": claimed_tokens}})

    # Mark nonce used only after successful insert
    if nonce:
        _mark_nonce_used(slug, nonce)

    # ✅ IMPORTANT: PRG redirect (prevents resubmission on refresh)
    return redirect(url_for("form_bp.submitted", slug=slug, rid=str(res.inserted_id)), code=303)


@form_bp.route("/f/<slug>/submitted", methods=["GET"])
def submitted(slug):
    db = current_app.mongo_db
    form = db["forms"].find_one({"slug": slug})
    if not form:
        return "Form not found", 404

    if form.get("suspended"):
        return render_template("runtime_form.html", form=form, suspended=True), 403

    rid = request.args.get("rid")  # optional receipt id
    return render_template("submitted.html", form=form, rid=rid)


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
        if fmap[fid].get("type") == "image":
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
    sub = subs_col.find_one({"_id": oid, "slug": slug})
    res = subs_col.delete_one({"_id": oid, "slug": slug})
    if res.deleted_count == 0:
        return jsonify({"ok": False, "error": "Submission not found"}), 404
    for value in (sub.get("fields") or {}).values():
        if isinstance(value, dict):
            delete_image(value)
    return jsonify({"ok": True})


@form_bp.route("/api/forms/<slug>/submissions/duplicates", methods=["GET"])
def find_duplicate_submissions(slug):
    db, forms_col, subs_col, form = _get_form_and_collections(slug)
    if not form:
        return jsonify({"ok": False, "error": "Form not found"}), 404

    fields_param = request.args.get("fields") or ""
    field_ids = [f.strip() for f in fields_param.split(",") if f.strip()]
    if not field_ids:
        return jsonify({"ok": False, "error": "No fields specified."}), 400

    valid_ids = {f.get("id") for f in (form.get("fields") or []) if f.get("type") != "image"}
    invalid = [f for f in field_ids if f not in valid_ids]
    if invalid:
        return jsonify({"ok": False, "error": f"Unknown field id(s): {', '.join(invalid)}"}), 400

    groups_map: dict[tuple, list] = {}
    cursor = subs_col.find({"slug": slug})
    for doc in cursor:
        all_fields = doc.get("fields") or {}
        key = tuple(((all_fields.get(fid, "") or "").strip().lower()) for fid in field_ids)
        if not any(key):
            continue

        entry = {
            "_id": str(doc.get("_id")),
            "created_at_str": doc.get("created_at").strftime("%Y-%m-%d %H:%M") if doc.get("created_at") else "",
            "fields": all_fields,
        }
        groups_map.setdefault(key, []).append(entry)

    groups = []
    duplicate_rows = 0
    for key, entries in groups_map.items():
        if len(entries) <= 1:
            continue
        kv = {}
        first_fields = entries[0]["fields"]
        for fid in field_ids:
            kv[fid] = first_fields.get(fid, "")
        groups.append({
            "key_values": kv,
            "count": len(entries),
            "submissions": entries,
        })
        duplicate_rows += len(entries)

    groups.sort(key=lambda g: g["count"], reverse=True)

    return jsonify({
        "ok": True,
        "field_ids": field_ids,
        "groups": groups,
        "duplicate_rows": duplicate_rows,
    })


@form_bp.route("/api/forms/<slug>/submissions/export", methods=["GET"])
def export_submissions(slug):
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
            row = [_export_value(doc.get("fields", {}).get(c, "")) for c in columns]
            writer.writerow(row)
            yield output.getvalue()
            output.seek(0); output.truncate(0)

    filename = f"{slug}-submissions.csv"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Type": "text/csv; charset=utf-8"
    }
    return Response(generate(), headers=headers)
