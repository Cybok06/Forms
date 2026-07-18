import io
import unittest
from unittest.mock import patch

from flask import Flask
from werkzeug.datastructures import FileStorage

from image_storage import ImageUploadError, MAX_IMAGE_BYTES, prepare_image_upload, upload_image


def make_upload(data, filename, content_type):
    return FileStorage(
        stream=io.BytesIO(data),
        filename=filename,
        content_type=content_type,
    )


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.ok = 200 <= status_code < 300

    def json(self):
        return self._body


class ImageStorageTests(unittest.TestCase):
    def test_accepts_heic_without_pillow_validation(self):
        upload = make_upload(b"phone camera bytes", "iphone-photo.heic", "application/octet-stream")

        prepared = prepare_image_upload(upload)

        self.assertEqual(prepared.content_type, "application/octet-stream")
        self.assertEqual(prepared.original_name, "iphone-photo.heic")
        self.assertEqual(prepared.size, len(b"phone camera bytes"))

    def test_rejects_non_image_without_image_extension(self):
        upload = make_upload(b"not an image", "notes.txt", "application/octet-stream")

        with self.assertRaises(ImageUploadError):
            prepare_image_upload(upload)

    def test_rejects_images_over_twenty_mb(self):
        upload = make_upload(b"x" * (MAX_IMAGE_BYTES + 1), "large.jpg", "image/jpeg")

        with self.assertRaises(ImageUploadError):
            prepare_image_upload(upload)

    def test_cloudflare_direct_upload_returns_delivery_url(self):
        app = Flask(__name__)
        upload = make_upload(b"image data", "camera.heic", "image/heic")
        responses = [
            FakeResponse(200, {"success": True, "result": {"id": "abc123", "uploadURL": "https://upload.example"}}),
            FakeResponse(200, {"success": True, "result": {"id": "abc123"}}),
        ]

        with app.app_context():
            with patch.dict(
                "os.environ",
                {
                    "CLOUDFLARE_ACCOUNT_ID": "account",
                    "CLOUDFLARE_IMAGES_TOKEN": "token",
                    "CLOUDFLARE_IMAGE_DELIVERY_HASH": "hash",
                    "CLOUDFLARE_IMAGE_VARIANT": "public",
                },
                clear=False,
            ), patch("image_storage.requests.post", side_effect=responses) as post:
                result = upload_image(upload)

        self.assertEqual(post.call_count, 2)
        self.assertEqual(result["storage"], "cloudflare")
        self.assertEqual(result["image_id"], "abc123")
        self.assertEqual(result["url"], "https://imagedelivery.net/hash/abc123/public")
        self.assertEqual(result["original_name"], "camera.heic")
        self.assertEqual(result["content_type"], "image/heic")


if __name__ == "__main__":
    unittest.main()
