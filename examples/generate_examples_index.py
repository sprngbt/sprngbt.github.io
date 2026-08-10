#!/usr/bin/env python3
"""Synchronise public Google Drive examples and rebuild examples_index.json.

The Drive folder is expected to contain one subfolder per example. Each example
folder contains a metadata .txt file, a MicroBlocks .ubp file, and any images or
other supporting files. Files are copied into this script's directory because
examples.html expects the existing flat ``examples/<filename>`` layout.

Install the optional Drive downloader before using a Drive URL:

    python3 -m pip install gdown

For testing, or when Google Drive for desktop is installed, ``--source`` may
instead point to a local folder.
"""

from __future__ import annotations

import argparse
import inspect
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
from typing import Iterator


EXAMPLES_DIR = Path(__file__).resolve().parent
JSON_FILE = EXAMPLES_DIR / "examples_index.json"
DEFAULT_DRIVE_URL = (
    "https://drive.google.com/drive/folders/"
    "1dNx-A6wRyNg9p4l6A_Ivn4bKsdiBNpTD?usp=share_link"
)


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


def download_drive_folder(url: str, destination: Path) -> Path:
    try:
        import gdown  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "Downloading a Google Drive URL requires gdown. Install it with: "
            "python3 -m pip install gdown"
        ) from exc

    options = {
        "url": url,
        "output": str(destination),
        "quiet": True,
        # The source is a public, link-shared folder. Avoid reading or creating
        # Google account cookies; this also keeps access limited to the URL.
        "use_cookies": False,
    }
    # gdown 5.x exposed this option; 6.x downloads all remaining files by
    # default and removed it. Supporting both avoids pinning users to one
    # particular release.
    if "remaining_ok" in inspect.signature(gdown.download_folder).parameters:
        options["remaining_ok"] = True
    downloaded = gdown.download_folder(**options)
    if not downloaded:
        raise RuntimeError("Google Drive returned no files")
    return find_source_root(destination)


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


def build_rows_and_copy(source_root: Path, dry_run: bool) -> list[dict]:
    rows = []
    destinations: dict[str, Path] = {}

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
            "file": ubp_path.name,
            "download": ubp_path.name,
            "example_name": ubp_path.stem,
            "author": metadata["author"],
            "version": metadata["version"],
            "description": metadata["description"],
            "codeimage": metadata["codeimage"],
            "storyimage": metadata["storyimage"],
            "url": metadata["url"],
            "files": metadata["files"],
        }
        rows.append(row)

        source_paths = sorted(
            example_dir.iterdir(), key=lambda path: path.name.lower()
        )
        for source_path in source_paths:
            if not source_path.is_file() or source_path.name.startswith("."):
                continue
            key = source_path.name.casefold()
            previous = destinations.get(key)
            if previous is not None and not same_file(previous, source_path):
                raise RuntimeError(
                    f"Cannot flatten Drive folders: both {previous} and {source_path} "
                    f"would become examples/{source_path.name}"
                )
            destinations[key] = source_path
            if not dry_run:
                shutil.copy2(source_path, EXAMPLES_DIR / source_path.name)

    rows.sort(key=lambda row: row["example_name"].lower())
    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        default=DEFAULT_DRIVE_URL,
        help="Public Google Drive folder URL or local synced/downloaded folder",
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
            temporary_directory = tempfile.TemporaryDirectory(prefix="springbot-examples-")
            source_root = download_drive_folder(
                args.source, Path(temporary_directory.name)
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
