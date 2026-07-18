import io
import os
from dataclasses import dataclass
from pathlib import Path

import requests
from bson import ObjectId
from flask import current_app, url_for
from gridfs import GridFS
from werkzeug.utils import secure_filename


MAX_IMAGE_BYTES = 20 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
    "image/heic",
    "image/heif",
    "image/heic-sequence",
    "image/heif-sequence",
    "application/octet-stream",
}
ALLOWED_IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".heic",
    ".heif",
}


class ImageUploadError(ValueError):
    pass


@dataclass(frozen=True)
class PreparedImageUpload:
    stream: io.BytesIO
    original_name: str
    content_type: str
    size: int


def _cloudflare_ready():
    return bool(os.getenv("CLOUDFLARE_ACCOUNT_ID") and _cloudflare_token())


def _cloudflare_token():
    return os.getenv("CLOUDFLARE_IMAGES_TOKEN") or os.getenv("CLOUDFLARE_API_TOKEN")


def _cloudinary_ready():
    if os.getenv("CLOUDINARY_URL"):
        return True
    return all(os.getenv(k) for k in ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET"))


def _configure_cloudinary():
    import cloudinary
    if not os.getenv("CLOUDINARY_URL"):
        cloudinary.config(
            cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
            api_key=os.getenv("CLOUDINARY_API_KEY"),
            api_secret=os.getenv("CLOUDINARY_API_SECRET"),
            secure=True,
        )


def _looks_like_image(filename, content_type):
    ext = Path(filename or "").suffix.lower()
    normalized_type = (content_type or "").split(";")[0].strip().lower()
    if normalized_type in ALLOWED_IMAGE_TYPES and (
        normalized_type.startswith("image/") or ext in ALLOWED_IMAGE_EXTENSIONS
    ):
        return True
    return ext in ALLOWED_IMAGE_EXTENSIONS


def _safe_error_from_cloudflare(body):
    errors = body.get("errors") if isinstance(body, dict) else None
    if isinstance(errors, list) and errors:
        message = errors[0].get("message") if isinstance(errors[0], dict) else None
        if message:
            return message
    return "Cloudflare could not upload the image. Please try again."


def _cloudflare_delivery_url(image_id):
    image_hash = os.getenv("CLOUDFLARE_IMAGE_DELIVERY_HASH")
    variant = os.getenv("CLOUDFLARE_IMAGE_VARIANT", "public")
    return f"https://imagedelivery.net/{image_hash}/{image_id}/{variant}"


def prepare_image_upload(file_obj):
    if not file_obj or not file_obj.filename:
        raise ImageUploadError("Choose an image to upload.")

    data = file_obj.read(MAX_IMAGE_BYTES + 1)
    file_obj.stream.seek(0)
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageUploadError("Image is too large. Maximum size is 20 MB.")
    if not data:
        raise ImageUploadError("The uploaded image is empty.")
    original_name = secure_filename(file_obj.filename or "") or "image"
    content_type = (file_obj.mimetype or file_obj.content_type or "application/octet-stream").split(";")[0].lower()
    if not _looks_like_image(original_name, content_type):
        raise ImageUploadError("Choose a JPG, PNG, WebP, GIF, HEIC, or HEIF image up to 20 MB.")

    return PreparedImageUpload(
        stream=io.BytesIO(data),
        original_name=original_name,
        content_type=content_type or "application/octet-stream",
        size=len(data),
    )


def validate_image(file_obj):
    prepare_image_upload(file_obj)


def upload_image(file_obj, folder="form_submissions"):
    prepared = prepare_image_upload(file_obj)
    original_name = prepared.original_name
    content_type = prepared.content_type

    if _cloudflare_ready():
        if not os.getenv("CLOUDFLARE_IMAGE_DELIVERY_HASH"):
            raise ImageUploadError("Cloudflare image delivery hash is not configured.")
        account_id = os.environ["CLOUDFLARE_ACCOUNT_ID"]
        direct_endpoint = (
            f"https://api.cloudflare.com/client/v4/accounts/"
            f"{account_id}/images/v2/direct_upload"
        )
        current_app.logger.info(
            "Image upload requested: filename=%s mimetype=%s size=%s",
            original_name,
            content_type,
            prepared.size,
        )
        direct_response = requests.post(
            direct_endpoint,
            headers={"Authorization": f"Bearer {_cloudflare_token()}"},
            data={"metadata": '{"source":"forms"}'},
            timeout=45,
        )
        current_app.logger.info("Cloudflare direct_upload status=%s", direct_response.status_code)
        try:
            direct_body = direct_response.json()
        except ValueError:
            direct_body = {}
        if not direct_response.ok or not direct_body.get("success"):
            raise ImageUploadError(_safe_error_from_cloudflare(direct_body))

        direct_result = direct_body.get("result") or {}
        upload_url = direct_result.get("uploadURL")
        image_id = direct_result.get("id")
        if not upload_url:
            raise ImageUploadError("Cloudflare did not return an upload URL.")

        prepared.stream.seek(0)
        upload_response = requests.post(
            upload_url,
            files={"file": (original_name, prepared.stream, content_type)},
            timeout=120,
        )
        current_app.logger.info("Cloudflare direct file upload status=%s", upload_response.status_code)
        try:
            upload_body = upload_response.json()
        except ValueError:
            upload_body = {}
        if not upload_response.ok or (isinstance(upload_body, dict) and upload_body.get("success") is False):
            raise ImageUploadError(_safe_error_from_cloudflare(upload_body))

        upload_result = upload_body.get("result") if isinstance(upload_body, dict) else {}
        if isinstance(upload_result, dict):
            image_id = upload_result.get("id") or image_id
        if not image_id:
            raise ImageUploadError("Cloudflare uploaded the file but did not return an image id.")

        current_app.logger.info("Cloudflare image upload completed: image_id=%s", image_id)
        return {
            "storage": "cloudflare",
            "image_id": image_id,
            "url": _cloudflare_delivery_url(image_id),
            "original_name": original_name,
            "content_type": content_type,
        }

    if _cloudinary_ready():
        import cloudinary.uploader

        _configure_cloudinary()
        prepared.stream.seek(0)
        uploaded = cloudinary.uploader.upload(
            prepared.stream,
            folder=folder,
            resource_type="image",
            overwrite=False,
        )
        return {
            "storage": "cloudinary",
            "public_id": uploaded.get("public_id"),
            "url": uploaded.get("secure_url") or uploaded.get("url"),
            "original_name": original_name,
            "content_type": content_type,
        }

    fs = GridFS(current_app.mongo_db)
    prepared.stream.seek(0)
    file_id = fs.put(prepared.stream, filename=original_name, content_type=content_type)
    return {
        "storage": "gridfs",
        "file_id": str(file_id),
        "url": url_for("form_bp.get_file", file_id=str(file_id)),
        "original_name": original_name,
        "content_type": content_type,
    }


def read_image(image_meta):
    if not isinstance(image_meta, dict):
        raise ImageUploadError("Image is unavailable.")
    if image_meta.get("storage") == "gridfs" and image_meta.get("file_id"):
        gridout = GridFS(current_app.mongo_db).get(ObjectId(image_meta["file_id"]))
        return gridout.read()
    url = image_meta.get("url")
    if not url:
        raise ImageUploadError("Image is unavailable.")
    response = requests.get(url, timeout=45)
    response.raise_for_status()
    return response.content


def delete_image(image_meta):
    if not isinstance(image_meta, dict):
        return
    try:
        storage = image_meta.get("storage")
        if storage == "gridfs" and image_meta.get("file_id"):
            GridFS(current_app.mongo_db).delete(ObjectId(image_meta["file_id"]))
        elif storage == "cloudinary" and image_meta.get("public_id") and _cloudinary_ready():
            import cloudinary.uploader
            _configure_cloudinary()
            cloudinary.uploader.destroy(image_meta["public_id"], resource_type="image")
        elif storage == "cloudflare" and image_meta.get("image_id") and _cloudflare_ready():
            requests.delete(
                f"https://api.cloudflare.com/client/v4/accounts/"
                f"{os.environ['CLOUDFLARE_ACCOUNT_ID']}/images/v1/{image_meta['image_id']}",
                headers={"Authorization": f"Bearer {_cloudflare_token()}"},
                timeout=30,
            )
    except Exception:
        current_app.logger.exception("Could not delete stored image")
