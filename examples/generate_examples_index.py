#!/usr/bin/env python3
"""Synchronise public Google Drive examples and rebuild examples_index.json.

The Drive folder is expected to contain one subfolder per example. Each example
folder contains a metadata .txt file, a MicroBlocks .ubp file, and any images or
other supporting files. That folder structure is preserved below ``examples``.

For a Drive URL, set ``GOOGLE_DRIVE_API_KEY`` to a Google Cloud API key that is
restricted to the Google Drive API. For authenticated, folder-scoped access,
set ``GOOGLE_APPLICATION_CREDENTIALS`` to a service-account JSON key and share
only the examples folder with that service account. For testing, ``--source``
may instead point to a local folder.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from typing import Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


EXAMPLES_DIR = Path(__file__).resolve().parent
JSON_FILE = EXAMPLES_DIR / "examples_index.json"
MANIFEST_FILE = EXAMPLES_DIR / ".drive_examples_manifest.json"
DEFAULT_DRIVE_URL = (
    "https://drive.google.com/drive/folders/"
    "1dNx-A6wRyNg9p4l6A_Ivn4bKsdiBNpTD?usp=share_link"
)
DRIVE_API_FILES_URL = "https://www.googleapis.com/drive/v3/files"
DRIVE_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"


def warn(message: str) -> None:
    print(f"Warning: {message}", file=sys.stderr)


def unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] in ("'", '"'):
        quote = value[0]
        if value[-1] == quote:
            return value[1:-1]
    return value


def parse_version_line(line: str) -> str:
    rest = line[len("version"):].strip()
    numbers = re.findall(r"\d+", rest)
    return ".".join(numbers) if numbers else rest


def parse_quoted_multiline(
    lines: list[str], start_index: int, keyword: str
) -> tuple[str, int]:
    line = lines[start_index].rstrip("\n")
    rest = line[len(keyword):].lstrip()
    if not rest:
        return "", start_index

    quote = rest[0]
    if quote not in ("'", '"'):
        return rest.strip(), start_index

    body = rest[1:]
    if body.endswith(quote):
        return body[:-1], start_index

    collected = [body]
    index = start_index + 1
    while index < len(lines):
        current = lines[index].rstrip("\n")
        if current.endswith(quote):
            collected.append(current[:-1])
            return "\n".join(collected).strip(), index
        collected.append(current)
        index += 1

    return "\n".join(collected).strip(), index - 1


def parse_simple_value(line: str, keyword: str) -> str:
    return unquote(line[len(keyword):].strip())


def parse_files_line(line: str) -> list[str]:
    """Parse bare or quoted filenames from a MicroBlocks-style files line."""
    rest = line[len("files"):].strip()
    if not rest:
        return []

    matches = re.findall(r"'([^']*)'|\"([^\"]*)\"|(\S+)", rest)
    files = []
    for single_quoted, double_quoted, bare in matches:
        value = (single_quoted or double_quoted or bare).strip()
        if value:
            files.append(value)
    return files


def parse_metadata_file(path: Path) -> dict:
    """Read metadata fields from a .txt file without inspecting the .ubp."""
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    data = {
        "author": "",
        "version": "",
        "description": "",
        "codeimage": "",
        "storyimage": "",
        "url": "",
        "files": [],
        "code": "",
    }

    saw_first_module = False
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        lowered = stripped.lower()

        if lowered.startswith("module "):
            if saw_first_module:
                break
            saw_first_module = True
            index += 1
            continue

        if saw_first_module:
            if lowered.startswith("author "):
                data["author"] = parse_simple_value(stripped, "author")
            elif lowered.startswith("version "):
                data["version"] = parse_version_line(stripped)
            elif lowered.startswith("description "):
                data["description"], index = parse_quoted_multiline(
                    lines, index, "description"
                )
            elif lowered.startswith("codeimage "):
                data["codeimage"] = parse_simple_value(stripped, "codeimage")
            elif lowered.startswith("storyimage "):
                data["storyimage"] = parse_simple_value(stripped, "storyimage")
            elif lowered == "url" or lowered.startswith("url "):
                data["url"] = parse_simple_value(stripped, "url")
            elif lowered == "files" or lowered.startswith("files "):
                data["files"] = parse_files_line(stripped)
            elif lowered.startswith("code "):
                data["code"] = parse_simple_value(stripped, "code")

        index += 1

    return data


def is_metadata_file(path: Path) -> bool:
    if path.suffix.lower() != ".txt":
        return False
    text = path.read_text(encoding="utf-8", errors="replace")
    return any(line.strip().lower().startswith("module ") for line in text.splitlines())


def choose_metadata_file(example_dir: Path) -> Path | None:
    expected = example_dir / f"{example_dir.name}.txt"
    if expected.is_file() and is_metadata_file(expected):
        return expected

    candidates = sorted(
        (path for path in example_dir.glob("*.txt") if is_metadata_file(path)),
        key=lambda path: path.name.lower(),
    )
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        warn(
            f"skipping {example_dir.name!r}: more than one metadata .txt file "
            f"was found ({', '.join(path.name for path in candidates)})"
        )
    else:
        warn(f"skipping {example_dir.name!r}: no metadata .txt file was found")
    return None


def choose_ubp_file(example_dir: Path, metadata: dict) -> Path | None:
    code_name = Path(metadata["code"]).name if metadata["code"] else ""
    if code_name:
        code_path = example_dir / code_name
        if code_path.is_file() and code_path.suffix.lower() == ".ubp":
            return code_path
        warn(
            f"{example_dir.name!r} names {code_name!r} in its Code line, "
            "but that .ubp file is missing"
        )

    expected = example_dir / f"{example_dir.name}.ubp"
    if expected.is_file():
        return expected

    candidates = sorted(example_dir.glob("*.ubp"), key=lambda path: path.name.lower())
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        warn(
            f"skipping {example_dir.name!r}: more than one .ubp file was found "
            f"({', '.join(path.name for path in candidates)})"
        )
    else:
        warn(f"skipping {example_dir.name!r}: no .ubp file was found")
    return None


def iter_example_dirs(source_root: Path) -> Iterator[Path]:
    for path in sorted(source_root.iterdir(), key=lambda item: item.name.lower()):
        if path.is_dir() and not path.name.startswith("."):
            yield path


def find_source_root(download_dir: Path) -> Path:
    """Handle downloaders that add one outer folder around the examples."""
    children = [path for path in download_dir.iterdir() if not path.name.startswith(".")]
    if len(children) == 1 and children[0].is_dir():
        return children[0]
    return download_dir


def extract_drive_folder_id(url: str) -> str:
    match = re.search(r"/folders/([A-Za-z0-9_-]+)", url)
    if not match:
        raise RuntimeError(f"Could not find a Google Drive folder ID in: {url}")
    return match.group(1)


def drive_api_request(
    endpoint: str,
    parameters: dict[str, str | int | bool],
    resource_key: str = "",
    access_token: str = "",
) -> bytes:
    url = f"{endpoint}?{urlencode(parameters)}"
    headers = {"User-Agent": "springbot-examples-index/1.0"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    if resource_key:
        file_id = endpoint.rstrip("/").rsplit("/", 1)[-1]
        headers["X-Goog-Drive-Resource-Keys"] = f"{file_id}/{resource_key}"

    try:
        with urlopen(Request(url, headers=headers), timeout=60) as response:
            return response.read()
    except HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace").strip()
        try:
            error = json.loads(details).get("error", {})
            message = error.get("message", "")
            reasons = {
                item.get("reason", "")
                for item in error.get("errors", [])
                if item.get("reason")
            }
            reasons.update(
                item.get("reason", "")
                for item in error.get("details", [])
                if isinstance(item, dict) and item.get("reason")
            )
            if reasons:
                message = f"{message} ({', '.join(sorted(reasons))})"
        except (ValueError, AttributeError):
            message = details
        if not message:
            message = str(exc)
        raise RuntimeError(
            f"Google Drive API returned HTTP {exc.code} for {endpoint}: {message}"
        ) from exc
    except URLError as exc:
        raise RuntimeError(f"Could not connect to the Google Drive API: {exc.reason}") from exc


def list_drive_folder(
    api_key: str, folder_id: str, access_token: str = ""
) -> list[dict]:
    items: list[dict] = []
    page_token = ""

    while True:
        parameters: dict[str, str | int | bool] = {
            "q": f"'{folder_id}' in parents and trashed = false",
            "fields": (
                "nextPageToken,"
                "files(id,name,mimeType,resourceKey,size,md5Checksum)"
            ),
            "pageSize": 1000,
            "orderBy": "name_natural",
            "spaces": "drive",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if api_key and not access_token:
            parameters["key"] = api_key
        if page_token:
            parameters["pageToken"] = page_token

        response = json.loads(
            drive_api_request(
                DRIVE_API_FILES_URL,
                parameters,
                access_token=access_token,
            ).decode("utf-8")
        )
        items.extend(response.get("files", []))
        page_token = response.get("nextPageToken", "")
        if not page_token:
            return items


def safe_drive_name(name: str) -> str:
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise RuntimeError(f"Google Drive returned an unsafe filename: {name!r}")
    return name


def file_md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_cached_file(item: dict, relative_path: Path) -> Path | None:
    candidates = [EXAMPLES_DIR / relative_path]
    if len(relative_path.parts) > 1:
        # Compatibility with the earlier version that copied every Drive file
        # directly into examples/ without its example-folder prefix.
        candidates.append(EXAMPLES_DIR / relative_path.name)

    expected_size = item.get("size", "")
    expected_md5 = item.get("md5Checksum", "")
    for candidate in candidates:
        if not candidate.is_file():
            continue
        if expected_size and candidate.stat().st_size != int(expected_size):
            continue
        if expected_md5 and file_md5(candidate) != expected_md5:
            continue
        return candidate
    return None


def download_drive_tree(
    api_key: str,
    access_token: str,
    folder_id: str,
    destination: Path,
    relative_path: Path,
    folder_items: list[dict] | None = None,
) -> int:
    downloaded_files = 0
    destination.mkdir(parents=True, exist_ok=True)

    if folder_items is None:
        try:
            folder_items = list_drive_folder(api_key, folder_id, access_token)
        except RuntimeError as exc:
            raise RuntimeError(
                f"Could not list Drive folder {destination.name!r}: {exc}"
            ) from exc

    for item in folder_items:
        name = safe_drive_name(item.get("name", ""))
        if name.startswith("."):
            continue
        item_id = item.get("id", "")
        mime_type = item.get("mimeType", "")
        resource_key = item.get("resourceKey", "")
        target = destination / name
        item_relative_path = relative_path / name

        if mime_type == DRIVE_FOLDER_MIME_TYPE:
            downloaded_files += download_drive_tree(
                api_key, access_token, item_id, target, item_relative_path
            )
            continue
        if mime_type.startswith("application/vnd.google-apps."):
            warn(f"skipping unsupported Google-native file {name!r}")
            continue

        cached_file = find_cached_file(item, item_relative_path)
        if cached_file is not None:
            shutil.copy2(cached_file, target)
            print(f"Unchanged, using local file: {item_relative_path.as_posix()}")
            downloaded_files += 1
            continue

        try:
            download_parameters: dict[str, str | bool] = {
                "alt": "media",
                "supportsAllDrives": "true",
            }
            if api_key and not access_token:
                download_parameters["key"] = api_key
            content = drive_api_request(
                f"{DRIVE_API_FILES_URL}/{item_id}",
                download_parameters,
                resource_key,
                access_token,
            )
        except RuntimeError as exc:
            raise RuntimeError(f"Could not download Drive file {name!r}: {exc}") from exc
        target.write_bytes(content)
        downloaded_files += 1

    return downloaded_files


def download_drive_folder(
    url: str, destination: Path, api_key: str, access_token: str
) -> Path:
    if not api_key and not access_token:
        raise RuntimeError(
            "Google Drive credentials are required. Set GOOGLE_APPLICATION_CREDENTIALS "
            "to a service-account JSON key, or set GOOGLE_DRIVE_API_KEY."
        )

    folder_id = extract_drive_folder_id(url)
    try:
        root_items = list_drive_folder(api_key, folder_id, access_token)
    except RuntimeError as exc:
        raise RuntimeError(f"Could not list the shared Drive folder: {exc}") from exc

    downloaded_files = 0
    for item in root_items:
        if item.get("mimeType") != DRIVE_FOLDER_MIME_TYPE:
            warn(f"skipping file outside an example folder: {item.get('name', '')!r}")
            continue

        folder_name = safe_drive_name(item.get("name", ""))
        if folder_name.casefold() == "template":
            print(f"Skipping template folder: {folder_name}")
            continue

        try:
            child_items = list_drive_folder(
                api_key, item.get("id", ""), access_token
            )
        except RuntimeError as exc:
            raise RuntimeError(
                f"Could not inspect Drive example folder {folder_name!r}: {exc}"
            ) from exc
        if not any(
            child.get("name", "").lower().endswith(".ubp")
            and child.get("mimeType") != DRIVE_FOLDER_MIME_TYPE
            for child in child_items
        ):
            warn(f"skipping {folder_name!r}: no .ubp file was found")
            continue

        downloaded_files += download_drive_tree(
            api_key,
            access_token,
            item.get("id", ""),
            destination / folder_name,
            Path(folder_name),
            child_items,
        )
    if downloaded_files == 0:
        raise RuntimeError(
            "Google Drive returned no files. Check that the folder and its contents "
            "are shared as 'Anyone with the link'."
        )
    return destination


def service_account_access_token(credentials_path: str) -> str:
    if not credentials_path:
        return ""

    path = Path(credentials_path).expanduser().resolve()
    if not path.is_file():
        raise RuntimeError(f"Service-account JSON file does not exist: {path}")
    try:
        from google.auth.transport.requests import Request as GoogleAuthRequest
        from google.oauth2 import service_account
    except ImportError as exc:
        raise RuntimeError(
            "Service-account authentication requires google-auth. Install it with: "
            "python -m pip install -r requirements.txt"
        ) from exc

    try:
        credentials = service_account.Credentials.from_service_account_file(
            str(path), scopes=[DRIVE_READONLY_SCOPE]
        )
        credentials.refresh(GoogleAuthRequest())
    except Exception as exc:
        raise RuntimeError(f"Could not authenticate the service account: {exc}") from exc
    if not credentials.token:
        raise RuntimeError("Google returned no service-account access token")
    return credentials.token


def resolve_local_source(value: str) -> Path:
    source = Path(value).expanduser().resolve()
    if not source.is_dir():
        raise RuntimeError(f"Source folder does not exist: {source}")
    return find_source_root(source)


def same_file(first: Path, second: Path) -> bool:
    return (
        first.stat().st_size == second.stat().st_size
        and first.read_bytes() == second.read_bytes()
    )


def validate_references(example_dir: Path, metadata: dict) -> None:
    references = [metadata["codeimage"], metadata["storyimage"], *metadata["files"]]
    for name in filter(None, references):
        if not (example_dir / Path(name).name).is_file():
            warn(f"{example_dir.name!r} references missing file {name!r}")


def prefixed_reference(example_dir: Path, name: str) -> str:
    if not name:
        return ""
    return (Path(example_dir.name) / Path(name).name).as_posix()


def safe_manifest_path(value: str) -> Path | None:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        return None
    return path


def load_manifest() -> set[Path]:
    if not MANIFEST_FILE.is_file():
        return set()
    try:
        values = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        warn(f"ignoring invalid sync manifest: {MANIFEST_FILE}")
        return set()
    paths = {safe_manifest_path(str(value)) for value in values}
    return {path for path in paths if path is not None}


def remove_empty_parents(path: Path) -> None:
    parent = path.parent
    while parent != EXAMPLES_DIR:
        try:
            parent.rmdir()
        except OSError:
            return
        parent = parent.parent


def update_manifest(copied_paths: set[Path]) -> None:
    for relative_path in load_manifest() - copied_paths:
        target = EXAMPLES_DIR / relative_path
        if target.is_file():
            target.unlink()
            remove_empty_parents(target)

    MANIFEST_FILE.write_text(
        json.dumps(
            sorted(path.as_posix() for path in copied_paths),
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def build_rows_and_copy(source_root: Path, dry_run: bool) -> list[dict]:
    rows = []
    copied_paths: set[Path] = set()

    for example_dir in iter_example_dirs(source_root):
        if example_dir.name.casefold() == "template":
            print(f"Skipping template folder: {example_dir.name}")
            continue

        metadata_path = choose_metadata_file(example_dir)
        if metadata_path is None:
            continue
        metadata = parse_metadata_file(metadata_path)
        ubp_path = choose_ubp_file(example_dir, metadata)
        if ubp_path is None:
            continue

        validate_references(example_dir, metadata)
        row = {
            "file": prefixed_reference(example_dir, ubp_path.name),
            "download": prefixed_reference(example_dir, ubp_path.name),
            "example_name": ubp_path.stem,
            "author": metadata["author"],
            "version": metadata["version"],
            "description": metadata["description"],
            "codeimage": prefixed_reference(example_dir, metadata["codeimage"]),
            "storyimage": prefixed_reference(example_dir, metadata["storyimage"]),
            "url": metadata["url"],
            "files": [
                prefixed_reference(example_dir, name) for name in metadata["files"]
            ],
        }
        rows.append(row)

        source_paths = sorted(example_dir.rglob("*"), key=lambda path: str(path).lower())
        for source_path in source_paths:
            source_relative = source_path.relative_to(example_dir)
            if (
                not source_path.is_file()
                or any(part.startswith(".") for part in source_relative.parts)
            ):
                continue
            destination_relative = Path(example_dir.name) / source_relative
            copied_paths.add(destination_relative)
            if not dry_run:
                destination = EXAMPLES_DIR / destination_relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, destination)

                # Migrate files copied by the earlier flat-layout version. Only
                # remove an old root file when its contents exactly match Drive.
                if len(source_relative.parts) == 1:
                    old_flat_path = EXAMPLES_DIR / source_path.name
                    if old_flat_path.is_file() and same_file(old_flat_path, source_path):
                        old_flat_path.unlink()

    rows.sort(key=lambda row: row["example_name"].lower())
    if not dry_run:
        update_manifest(copied_paths)
    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        default=DEFAULT_DRIVE_URL,
        help="Public Google Drive folder URL or local synced/downloaded folder",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("GOOGLE_DRIVE_API_KEY", ""),
        help="Google Drive API key (prefer the GOOGLE_DRIVE_API_KEY environment variable)",
    )
    parser.add_argument(
        "--service-account",
        default=os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", ""),
        help=(
            "Path to service-account JSON credentials "
            "(prefer GOOGLE_APPLICATION_CREDENTIALS)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print what would change without copying or writing JSON",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    temporary_directory: tempfile.TemporaryDirectory[str] | None = None

    try:
        if re.match(r"^https?://", args.source, flags=re.IGNORECASE):
            access_token = service_account_access_token(args.service_account)
            temporary_directory = tempfile.TemporaryDirectory(prefix="springbot-examples-")
            source_root = download_drive_folder(
                args.source,
                Path(temporary_directory.name),
                args.api_key,
                access_token,
            )
        else:
            source_root = resolve_local_source(args.source)

        rows = build_rows_and_copy(source_root, args.dry_run)
        if not args.dry_run:
            JSON_FILE.write_text(
                json.dumps(rows, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

        print(f"Source folder: {source_root}")
        print(f"Found valid examples: {len(rows)}")
        print(f"Output folder: {EXAMPLES_DIR}")
        if args.dry_run:
            print("Dry run: no files were changed")
        else:
            print(f"Wrote: {JSON_FILE}")
    finally:
        if temporary_directory is not None:
            temporary_directory.cleanup()


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1)
