import os
import re

from fastapi import APIRouter
from fastapi.responses import FileResponse


router = APIRouter(prefix="/cases", tags=["cases"])

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
OUTBREAKER_IMAGE_RE = re.compile(r"^outbreaker_[A-Za-z0-9_.-]+\.png$")


def _export_path(*parts: str) -> str:
    """Return an absolute path under the repository export directory."""
    return os.path.join(PROJECT_ROOT, "exports", *parts)


@router.get("/outbreaker-image/{filename}")
def get_outbreaker_image(filename: str):
    """Serve outbreaker2 generated graphics."""
    # Security: only serve expected outbreaker2 images.
    if not OUTBREAKER_IMAGE_RE.fullmatch(filename):
        return {"error": "Invalid file"}

    path = _export_path(filename)
    if os.path.exists(path):
        return FileResponse(path, media_type="image/png")

    return {"error": "Image not found"}
