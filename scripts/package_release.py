from __future__ import annotations

import argparse
import os
import zipfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_OUTPUT = _REPO_ROOT / "dp_document_bot.zip"
_ARCHIVE_ROOT = "dp_document_bot"

_EXCLUDED_DIR_NAMES = frozenset(
    {
        ".venv",
        ".git",
        "__pycache__",
        "CDP_Profile",
        "data",
        ".pytest_cache",
        ".cursor",
        ".mypy_cache",
        ".ruff_cache",
        "htmlcov",
    }
)
_EXCLUDED_SUFFIXES = frozenset({".pyc", ".sqlite3", ".db", ".log"})
_EXCLUDED_FILE_NAMES = frozenset({".env", "dp_document_bot.zip"})


def should_exclude(path: Path, *, root: Path) -> bool:
    relative = path.relative_to(root)
    if any(part in _EXCLUDED_DIR_NAMES for part in relative.parts[:-1]):
        return True
    if path.is_dir() and path.name in _EXCLUDED_DIR_NAMES:
        return True
    if path.name in _EXCLUDED_FILE_NAMES:
        return True
    if path.suffix in _EXCLUDED_SUFFIXES:
        return True
    return False


def iter_release_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in _EXCLUDED_DIR_NAMES]
        current = Path(dirpath)
        for filename in filenames:
            path = current / filename
            if should_exclude(path, root=root):
                continue
            files.append(path)
    return sorted(files)


def build_release_zip(root: Path, output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in iter_release_files(root):
            archive_name = Path(_ARCHIVE_ROOT) / path.relative_to(root)
            archive.write(path, arcname=archive_name.as_posix())
    return output


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a clean client ZIP without local secrets, venv, or state."
    )
    parser.add_argument(
        "--output",
        default=str(_DEFAULT_OUTPUT),
        help="ZIP path (default: <repo>/dp_document_bot.zip)",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    output = Path(args.output)
    if not output.is_absolute():
        output = _REPO_ROOT / output
    built = build_release_zip(_REPO_ROOT, output.resolve())
    print(f"wrote {built}")


if __name__ == "__main__":
    main()
