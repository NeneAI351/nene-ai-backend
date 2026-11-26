"""Provider adapter for editing one uploaded still image."""

import base64
import binascii
import os

import httpx

from media_storage import MAX_SOURCE_IMAGE_BYTES, validate_image_bytes


class PhotoEditError(RuntimeError):
    """The provider did not return a usable edited photo."""


async def edit_photo(client: httpx.AsyncClient, image: bytes, prompt: str) -> bytes:
    content_type, extension = validate_image_bytes(image)
    prompt = prompt.strip()
    if not 1 <= len(prompt) <= 2000:
        raise ValueError("Describe your photo edit in 1 to 2,000 characters.")

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    model = os.getenv("NENE_PHOTO_EDIT_MODEL", "").strip()
    if not api_key or not model:
        raise PhotoEditError("Photo editing is unavailable.")
    try:
        response = await client.post(
            "https://api.openai.com/v1/images/edits",
            headers={"Authorization": f"Bearer {api_key}"},
            data={
                "model": model,
                "prompt": prompt,
                "n": "1",
                "quality": "medium",
                "output_format": "webp",
            },
            files={"image[]": (f"photo.{extension}", image, content_type)},
            timeout=120,
            follow_redirects=False,
        )
        response.raise_for_status()
        encoded = response.json()["data"][0]["b64_json"]
        if len(encoded) > 4 * ((MAX_SOURCE_IMAGE_BYTES + 2) // 3):
            raise ValueError("Edited image exceeds storage limit.")
        result = base64.b64decode(encoded, validate=True)
        result_type, _ = validate_image_bytes(result)
        if result_type != "image/webp":
            raise ValueError("Unexpected edited image format.")
        return result
    except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError, binascii.Error):
        # Never include provider bodies, credentials, or submitted prompts in errors.
        raise PhotoEditError("The photo edit could not be completed.") from None
