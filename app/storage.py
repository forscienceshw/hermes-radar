import hashlib
import os
from pathlib import Path

from .paths import BLOB_DIR, DOCUMENT_DIR
from .utils import content_sha256, safe_name


def store_blob(data: bytes, suffix: str = "") -> tuple[str, Path]:
    digest = content_sha256(data)
    suffix = suffix if suffix.startswith(".") or not suffix else "." + suffix
    folder = BLOB_DIR / digest[:2]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{digest}{suffix[:12]}"
    if not path.exists():
        temporary = path.with_suffix(path.suffix + ".part")
        temporary.write_bytes(data)
        os.replace(temporary, path)
    return digest, path


def store_blob_file(source: Path, suffix: str = "") -> tuple[str, Path]:
    """Hash and store an existing large file without loading it into memory."""
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    key = digest.hexdigest()
    suffix = suffix if suffix.startswith(".") or not suffix else "." + suffix
    folder = BLOB_DIR / key[:2]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{key}{suffix[:12]}"
    if not path.exists():
        temporary = path.with_suffix(path.suffix + ".part")
        with source.open("rb") as reader, temporary.open("wb") as writer:
            for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                writer.write(chunk)
        os.replace(temporary, path)
    return key, path


def item_directory(item_id: str) -> Path:
    path = DOCUMENT_DIR / item_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def atomic_write(path: Path, data: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    if isinstance(data, str):
        temporary.write_text(data, encoding="utf-8")
    else:
        temporary.write_bytes(data)
    os.replace(temporary, path)
    return path


def paper_folder(doi: str) -> Path:
    from .paths import DATA_DIR
    path = DATA_DIR / "papers" / safe_name(doi.replace("/", "_"))
    path.mkdir(parents=True, exist_ok=True)
    return path
