"""Gemini-powered picture description enricher.

Calls Google Gemini (default: gemini-3.1-flash) on each picture in a
DoclingDocument and writes the returned caption back into the document.

Two backends are supported via the `google-genai` SDK:

1. Gemini Developer API (default). Requires GEMINI_API_KEY.
2. Vertex AI. Requires GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION,
   plus Application Default Credentials. Enabled by setting
   GOOGLE_GENAI_USE_VERTEXAI=true (auto-detected when the API key is
   absent and GOOGLE_CLOUD_PROJECT is set, which is the Cloud Run default).

Install with: pip install opendataloader-pdf[gemini]
"""

from __future__ import annotations

import base64
import io
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_GEMINI_MODEL = "gemini-3.1-flash"
DEFAULT_GEMINI_PROMPT = (
    "Describe what you see in this image. Include any text, numbers, labels, "
    "axis titles, legends, and data values visible. Be concise and factual; "
    "do not speculate beyond what is shown."
)


@dataclass
class GeminiConfig:
    """Runtime configuration for the Gemini enricher."""

    api_key: Optional[str] = None
    model: str = DEFAULT_GEMINI_MODEL
    prompt: str = DEFAULT_GEMINI_PROMPT
    max_output_tokens: int = 512
    temperature: float = 0.0
    use_vertexai: bool = False
    project: Optional[str] = None
    location: Optional[str] = None
    timeout_seconds: float = 30.0

    @classmethod
    def from_env(cls, **overrides: Any) -> "GeminiConfig":
        """Build a config from environment variables plus explicit overrides."""
        api_key = overrides.pop("api_key", None) or os.environ.get("GEMINI_API_KEY")
        project = overrides.pop("project", None) or os.environ.get("GOOGLE_CLOUD_PROJECT")
        location = (
            overrides.pop("location", None)
            or os.environ.get("GOOGLE_CLOUD_LOCATION")
            or "us-central1"
        )

        env_flag = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in {"1", "true", "yes"}
        use_vertexai = overrides.pop("use_vertexai", None)
        if use_vertexai is None:
            use_vertexai = env_flag or (not api_key and bool(project))

        return cls(
            api_key=api_key,
            project=project,
            location=location,
            use_vertexai=bool(use_vertexai),
            **overrides,
        )


class GeminiEnricher:
    """Generates picture descriptions by calling Gemini on each image.

    The client is created lazily on first use so that constructing the enricher
    (at server startup) never fails when credentials are absent — the failure
    surfaces only when a request actually tries to use it.
    """

    def __init__(self, config: GeminiConfig):
        self.config = config
        self._client: Any = None
        self._client_lock = threading.Lock()

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        with self._client_lock:
            if self._client is not None:
                return self._client
            try:
                from google import genai  # type: ignore
            except ImportError as exc:
                raise ImportError(
                    "google-genai is not installed. Install with: "
                    "pip install opendataloader-pdf[gemini]"
                ) from exc

            if self.config.use_vertexai:
                if not self.config.project:
                    raise ValueError(
                        "Vertex AI mode requires GOOGLE_CLOUD_PROJECT (or --gemini-project)."
                    )
                self._client = genai.Client(
                    vertexai=True,
                    project=self.config.project,
                    location=self.config.location,
                )
                logger.info(
                    "Gemini client: Vertex AI (project=%s, location=%s, model=%s)",
                    self.config.project,
                    self.config.location,
                    self.config.model,
                )
            else:
                if not self.config.api_key:
                    raise ValueError(
                        "Gemini API key missing. Set GEMINI_API_KEY or pass --gemini-api-key."
                    )
                self._client = genai.Client(api_key=self.config.api_key)
                logger.info("Gemini client: Developer API (model=%s)", self.config.model)
            return self._client

    def describe_image(self, image_bytes: bytes, mime_type: str = "image/png") -> str:
        """Call Gemini with a single image and return the generated caption."""
        from google.genai import types  # type: ignore

        client = self._get_client()
        contents = [
            types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            self.config.prompt,
        ]
        generation_config = types.GenerateContentConfig(
            temperature=self.config.temperature,
            max_output_tokens=self.config.max_output_tokens,
        )

        response = client.models.generate_content(
            model=self.config.model,
            contents=contents,
            config=generation_config,
        )
        text = getattr(response, "text", None)
        if not text:
            return ""
        return text.strip()

    def enrich_document(self, json_content: dict) -> dict:
        """Add Gemini-generated annotations to every picture in the document.

        The input is the dict produced by DoclingDocument.export_to_dict().
        Pictures without embedded image data are skipped with a warning.
        Returns the same dict (mutated in place) for convenience.
        """
        pictures = json_content.get("pictures") or []
        if not pictures:
            return json_content

        total = 0
        enriched = 0
        failed = 0
        started = time.perf_counter()

        for picture in pictures:
            total += 1
            image_bytes, mime_type = _extract_picture_bytes(picture)
            if image_bytes is None:
                logger.debug("Skipping picture without embedded image data")
                continue
            try:
                caption = self.describe_image(image_bytes, mime_type=mime_type)
            except Exception as exc:  # Log and continue; one bad image shouldn't kill the batch
                failed += 1
                logger.warning("Gemini description failed for a picture: %s", exc)
                continue
            if not caption:
                continue
            annotations = picture.setdefault("annotations", [])
            annotations.append(
                {
                    "kind": "description",
                    "text": caption,
                    "provenance": f"gemini:{self.config.model}",
                }
            )
            enriched += 1

        elapsed = time.perf_counter() - started
        logger.info(
            "Gemini enrichment: %d/%d pictures captioned (failed=%d) in %.2fs",
            enriched,
            total,
            failed,
            elapsed,
        )
        return json_content


def _extract_picture_bytes(picture: dict) -> tuple[Optional[bytes], str]:
    """Pull raw image bytes out of a DoclingDocument picture node.

    Docling exports embedded images either as a data URI
    (`image.uri = "data:image/png;base64,..."`) or as a PIL-encoded dict.
    """
    image = picture.get("image") or {}
    uri = image.get("uri")
    if isinstance(uri, str) and uri.startswith("data:"):
        header, _, payload = uri.partition(",")
        mime_type = "image/png"
        if ";" in header:
            mime_type = header.split(":", 1)[1].split(";", 1)[0] or mime_type
        try:
            return base64.b64decode(payload), mime_type
        except (ValueError, TypeError):
            return None, mime_type

    pil_bytes = image.get("bytes")
    if isinstance(pil_bytes, (bytes, bytearray)):
        return bytes(pil_bytes), image.get("mimetype", "image/png")

    # Some Docling versions store the image as a PIL.Image — re-encode to PNG.
    pil_image = image.get("pil_image") or image.get("pil")
    if pil_image is not None:
        try:
            buf = io.BytesIO()
            pil_image.save(buf, format="PNG")
            return buf.getvalue(), "image/png"
        except Exception:
            return None, "image/png"

    return None, "image/png"
