"""Check an export for problems that would break it in Obsidian / Obsidian Bases.

Usage: python verify_export.py [export_dir] [attachments_dir]
(defaults: export, <export_dir>/_attachments)
"""

import collections
import datetime
import re
import sys
import unicodedata
from pathlib import Path

import yaml

LINK_RE = re.compile(r"!?\[\[([^\]]+)\]\]")
URL_RE = re.compile(r"https?://")
BAD_NAME_CHARS = set("#^[]|\\/:")


def key(name):
    return unicodedata.normalize("NFC", name).casefold()


def type_name(value):
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, datetime.datetime):
        return "datetime"
    if isinstance(value, datetime.date):
        return "date"
    if isinstance(value, list):
        inner = sorted({type_name(v) for v in value})
        return f"list[{'/'.join(inner)}]"
    return type(value).__name__


def strings(value):
    if isinstance(value, list):
        for v in value:
            yield from strings(v)
    elif isinstance(value, str):
        yield value


def main():
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "export")
    att_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else root / "_attachments"

    attachments = [p for p in att_dir.iterdir() if p.is_file()] if att_dir.exists() else []
    notes = sorted(p for p in root.rglob("*.md") if att_dir not in p.parents)
    note_keys = {key(p.stem) for p in notes}
    att_keys = {key(p.name) for p in attachments}
    att_names = {p.name for p in attachments}

    no_frontmatter, bad_names, ws_props, dangling = [], [], [], []
    by_name = collections.defaultdict(list)
    types = collections.defaultdict(lambda: collections.defaultdict(set))
    note_props = []  # (note, frontmatter) for the URL check below
    leftover_parts = [p.name for p in attachments if p.name.endswith(".part")]
    no_extension = [p.name for p in attachments if not re.search(r"\.[A-Za-z0-9]{1,5}$", p.name)]

    for note in notes:
        rel = note.relative_to(root)
        by_name[key(note.stem)].append(str(rel))
        if BAD_NAME_CHARS & set(note.stem):
            bad_names.append(str(rel))

        text = note.read_text(encoding="utf-8")
        match = re.match(r"---\n(.*?)^---\n", text, re.S | re.M)
        fm = yaml.safe_load(match.group(1)) if match else None
        if not isinstance(fm, dict) or not fm:
            no_frontmatter.append(str(rel))
            fm = {}
        note_props.append((rel, fm))

        table = str(rel.parent)
        for prop, value in fm.items():
            if prop != prop.strip():
                ws_props.append(f"{table}: {prop!r}")
            types[table][prop].add(type_name(value))

        # Links from parsed values (YAML may escape quotes in the raw text) + body.
        body = text[match.end():] if match else text
        link_sources = [*(s for v in fm.values() for s in strings(v)), body]
        for target in (t for src in link_sources for t in LINK_RE.findall(src)):
            target = target.split("|")[0].split("#")[0]
            if key(target) not in note_keys and key(target) not in att_keys:
                dangling.append(f"{rel}: [[{target}]]")

    # Attachment properties = any property that embeds a file from the attachments folder.
    att_props = {
        (str(rel.parent), prop)
        for rel, fm in note_props
        for prop, value in fm.items()
        if any(m in att_names for s in strings(value) for m in LINK_RE.findall(s))
    }
    url_in_att = [
        f"{rel}: {prop}"
        for rel, fm in note_props
        for prop, value in fm.items()
        if (str(rel.parent), prop) in att_props and any(URL_RE.search(s) for s in strings(value))
    ]
    airtable_urls = [
        str(rel) for rel, fm in note_props
        if any("airtableusercontent" in s for v in fm.values() for s in strings(v))
    ]
    duplicates = {k: v for k, v in by_name.items() if len(v) > 1}

    def section(title, items):
        print(f"\n## {title}: {len(items) if items else 'none'}")
        for item in list(items)[:50]:
            print(f"  - {item}")

    print(f"# Export check: {root}/  ({len(notes)} notes)")
    section("Notes without frontmatter", no_frontmatter)
    section("Duplicate note names", [", ".join(v) for v in duplicates.values()])
    section("Dangling wikilinks", dangling)
    section("Note names with # ^ [ ] | \\ / :", bad_names)
    section("Property names with leading/trailing whitespace", ws_props)
    section("http(s) URLs in attachment properties", url_in_att)
    section("airtableusercontent URLs anywhere", airtable_urls)
    section("Leftover partial downloads", leftover_parts)
    section("Attachments without a file extension", no_extension)

    print("\n## Properties per table")
    for table in sorted(types):
        print(f"  {table}")
        for prop, kinds in types[table].items():
            print(f"    {prop}: {', '.join(sorted(kinds))}")

    print(f"\n## Attachment properties: {', '.join(sorted(f'{t} -> {p}' for t, p in att_props)) or 'none'}")
    total = sum(p.stat().st_size for p in attachments)
    print(f"## Attachments: {len(attachments)} files, {total / 1_048_576:.1f} MB in {att_dir}/")


if __name__ == "__main__":
    main()
