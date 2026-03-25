#!/usr/bin/env python3
from pathlib import Path
import json
import re

LIBRARY_DIR = Path(__file__).resolve().parent
JSON_FILE = LIBRARY_DIR / "library_index.json"

def parse_module_line(line: str):
    rest = line[len("module"):].strip()
    module_type = ""
    module_name = ""

    if not rest:
        return module_name, module_type

    if rest[0] in ("'", '"'):
        q = rest[0]
        end = rest.find(q, 1)
        if end != -1:
            module_name = rest[1:end]
            module_type = rest[end + 1:].strip()
        else:
            module_name = rest[1:].strip()
    else:
        parts = rest.split()
        if len(parts) >= 2:
            module_type = parts[-1]
            module_name = " ".join(parts[:-1])
        elif parts:
            module_name = parts[0]

    return module_name.strip(), module_type.strip()

def parse_author_line(line: str) -> str:
    rest = line[len("author"):].strip()
    if rest[:1] in ("'", '"'):
        q = rest[0]
        end = rest.find(q, 1)
        if end != -1:
            return rest[1:end]
        return rest[1:]
    return rest

def parse_version_line(line: str) -> str:
    rest = line[len("version"):].strip()
    nums = re.findall(r"\d+", rest)
    return ".".join(nums)

def parse_description(lines, start_index):
    line = lines[start_index].rstrip("\n")
    rest = line[len("description"):].lstrip()
    if not rest:
        return "", start_index

    quote = rest[0]
    if quote not in ("'", '"'):
        return rest.strip(), start_index

    body = rest[1:]
    collected = []

    if body.endswith(quote):
        return body[:-1], start_index

    collected.append(body)
    i = start_index + 1
    while i < len(lines):
        current = lines[i].rstrip("\n")
        if current.endswith(quote):
            collected.append(current[:-1])
            return "\n".join(collected).strip(), i
        collected.append(current)
        i += 1

    return "\n".join(collected).strip(), i - 1

def parse_ubl_file(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    module_name = ""
    module_type = ""
    author = ""
    version = ""
    description = ""

    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if s.startswith("module "):
            module_name, module_type = parse_module_line(s)
        elif s.startswith("author "):
            author = parse_author_line(s)
        elif s.startswith("version "):
            version = parse_version_line(s)
        elif s.startswith("description "):
            description, i = parse_description(lines, i)
        i += 1

    return {
        "file": path.name,
        "download": path.name,
        "module_name": module_name or path.stem,
        "module_type": module_type,
        "author": author,
        "version": version,
        "description": description.strip(),
    }

def main():
    rows = []

    for path in sorted(LIBRARY_DIR.glob("*.ubl"), key=lambda p: p.name.lower()):
        rows.append(parse_ubl_file(path))

    rows.sort(key=lambda r: r["module_name"].lower())
    JSON_FILE.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Scanned folder: {LIBRARY_DIR}")
    print(f"Found .ubl files: {len(rows)}")
    print(f"Wrote: {JSON_FILE}")

if __name__ == "__main__":
    main()
