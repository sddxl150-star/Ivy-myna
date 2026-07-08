"""
File upload endpoint.
"""
import random
import string
import time
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, Request
from fastapi.responses import JSONResponse

from paths import UPLOADS_DIR

router = APIRouter()

UPLOAD_DIR = UPLOADS_DIR
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

ARCHIVE_SUFFIXES = (
    ".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".tar.gz", ".bz2", ".tbz", ".tar.bz2", ".xz", ".txz", ".tar.xz"
)
ARCHIVE_MIME_TYPES = {
    "application/zip",
    "application/x-zip-compressed",
    "application/x-7z-compressed",
    "application/vnd.rar",
    "application/x-rar-compressed",
    "application/x-tar",
    "application/gzip",
    "application/x-gzip",
    "application/x-bzip2",
    "application/x-xz",
}


def _safe_upload_suffix(filename: str | None) -> str:
    """Preserve normal and compound archive suffixes without trusting the name."""
    if not filename:
        return ""
    lower_name = filename.lower()
    for suffix in ARCHIVE_SUFFIXES:
        if lower_name.endswith(suffix):
            return suffix
    return Path(filename).suffix


def _upload_type(content_type: str | None, filename: str | None) -> str:
    if content_type and content_type.startswith("image/"):
        return "image"
    lower_name = (filename or "").lower()
    if (content_type in ARCHIVE_MIME_TYPES) or any(lower_name.endswith(s) for s in ARCHIVE_SUFFIXES):
        return "archive"
    return "file"


@router.post("/admin/upload")
async def upload_file(request: Request, file: UploadFile = File(...)):
    ext = _safe_upload_suffix(file.filename)
    rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    filename = f"{int(time.time())}-{rand}{ext}"
    filepath = UPLOAD_DIR / filename
    tmp_path = filepath.with_suffix(filepath.suffix + ".tmp")

    size = 0
    try:
        # Stream to disk instead of reading the whole file into memory. Large
        # archives previously failed easily because they were buffered entirely
        # in the backend process before writing.
        with tmp_path.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                out.write(chunk)
        tmp_path.replace(filepath)
    except Exception as exc:
        tmp_path.unlink(missing_ok=True)
        return JSONResponse({"ok": False, "error": f"文件保存失败: {exc}"}, status_code=500)
    finally:
        try:
            await file.close()
        except Exception:
            pass

    return {
        "ok": True,
        "url": f"/uploads/{filename}",
        "type": _upload_type(file.content_type, file.filename),
        "name": file.filename,
        "size": size,
    }
