#!/usr/bin/env python3
from pathlib import Path
import json
import re

EXAMPLES_DIR = Path(__file__).resolve().parent
JSON_FILE = EXAMPLES_DIR / "examples_index.json"


def unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] in ("'", '"'):
        q = value[0]
        if value[-1] == q:
            return value[1:-1]
    return value


def parse_version_line(line: str) -> str:
    rest = line[len("version"):].strip()
    nums = re.findall(r"\d+", rest)
    return ".".join(nums) if nums else rest


def parse_quoted_multiline(lines: list[str], start_index: int, keyword: str) -> tuple[str, int]:
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
    i = start_index + 1
    while i < len(lines):
        current = lines[i].rstrip("\n")
        if current.endswith(quote):
            collected.append(current[:-1])
            return "\n".join(collected).strip(), i
        collected.append(current)
        i += 1

    return "\n".join(collected).strip(), i - 1


def parse_simple_value(line: str, keyword: str) -> str:
    return unquote(line[len(keyword):].strip())


def parse_files_line(line: str) -> list[str]:
    """Parse a MicroBlocks-style files line.

    Expected examples:
      files qr.png happy.csv
      files 'my file.pdf' "second file.txt"

    Quoted filenames with spaces are supported. Bare filenames are split on
    whitespace. Empty values are ignored.
    """
    rest = line[len("files"):].strip()
    if not rest:
        return []

    matches = re.findall(r"'([^']*)'|\"([^\"]*)\"|(\S+)", rest)
    files = []
    for single_quoted, double_quoted, bare in matches:
        value = single_quoted or double_quoted or bare
        value = value.strip()
        if value:
            files.append(value)
    return files


def parse_ubp_file(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    data = {
        "file": path.name,
        "download": path.name,
        "example_name": path.stem,
        "author": "",
        "version": "",
        "description": "",
        "codeimage": "",
        "storyimage": "",
        "url": "",
        "files": [],
    }

    saw_first_module = False
    i = 0
    while i < len(lines):
        s = lines[i].strip()

        # The first module is the example itself. Later modules are imported libraries,
        # so stop when a second module begins.
        if s.startswith("module "):
            if saw_first_module:
                break
            saw_first_module = True
            i += 1
            continue

        if saw_first_module:
            if s.startswith("author "):
                data["author"] = parse_simple_value(s, "author")
            elif s.startswith("version "):
                data["version"] = parse_version_line(s)
            elif s.startswith("description "):
                data["description"], i = parse_quoted_multiline(lines, i, "description")
            elif s.startswith("codeimage "):
                data["codeimage"] = parse_simple_value(s, "codeimage")
            elif s.startswith("storyimage "):
                data["storyimage"] = parse_simple_value(s, "storyimage")
            elif s.startswith("url "):
                data["url"] = parse_simple_value(s, "url")
            elif s.startswith("files "):
                data["files"] = parse_files_line(s)

        i += 1

    return data


def main() -> None:
    rows = []

    for path in sorted(EXAMPLES_DIR.glob("*.ubp"), key=lambda p: p.name.lower()):
        rows.append(parse_ubp_file(path))

    rows.sort(key=lambda row: row["example_name"].lower())
    JSON_FILE.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Scanned folder: {EXAMPLES_DIR}")
    print(f"Found .ubp files: {len(rows)}")
    print(f"Wrote: {JSON_FILE}")


if __name__ == "__main__":
    main()
