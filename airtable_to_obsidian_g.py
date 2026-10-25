"""Export every accessible Airtable base into Obsidian-ready Markdown.

Discovers all bases and tables via the Airtable metadata API (using pyairtable),
reads each table's schema so the primary field and field types are known, and
writes one Markdown file per record laid out as:

    <output_dir>/<base name>/<table name>/<record>.md

Each note gets YAML frontmatter with one typed property per Airtable field
(numbers, checkboxes, dates, lists and [[wikilinks]] kept as their real types)
so the export works directly with Obsidian Bases. Linked records resolve to the
linked record's note name. Attachments are downloaded into one shared folder
and referenced as [[file]] embeds, since Airtable's attachment URLs expire.
The note body is configurable (see A2O_BODY in .env.sample).
"""

import datetime
import mimetypes
import os
import unicodedata
import urllib.request
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pyairtable import Api

# Characters that break an Obsidian [[wikilink]]: '#' starts a heading link (it is
# dropped), the rest are link syntax or path separators (replaced with '-').
LINK_DROP_CHARS = set("#")
LINK_DASH_CHARS = set("^[]|\\/:")
# Not allowed in file/folder names on Windows.
INVALID_CHARS = set('*?"<>')

# Code point ranges treated as emoji when A2O_STRIP_EMOJI is on: pictographs,
# misc symbols/dingbats, a few watch/hourglass symbols, plus the joiners and
# variation selectors that glue emoji sequences together.
EMOJI_RANGES = (
    (0x1F000, 0x1FAFF), (0x2600, 0x27BF), (0x2B00, 0x2BFF),
    (0x231A, 0x231B), (0x23E9, 0x23F3), (0x23F8, 0x23FA),
    (0xFE00, 0xFE0F), (0x200D, 0x200D), (0x20E3, 0x20E3), (0xE0020, 0xE007F),
)

# Note body modes for A2O_BODY; any other value names a field to use as the body.
BODY_FIELDS = "fields"
BODY_NONE = "none"

# Field types whose values we intentionally do not export.
#   button / externalSyncSource : not real content
#   createdBy / lastModifiedBy  : collaborator audit fields (contain emails -> PII)
SKIP_FIELD_TYPES = {"button", "externalSyncSource", "createdBy", "lastModifiedBy"}

# Remember unknown field types we've warned about so we only warn once each.
_warned_types = set()


# --------------------------------------------------------------------------- #
# Sanitization helpers
# --------------------------------------------------------------------------- #
def strip_emoji(name):
    """Remove emoji, keeping the original if nothing else is left."""
    stripped = "".join(
        c for c in name if not any(lo <= ord(c) <= hi for lo, hi in EMOJI_RANGES)
    )
    stripped = " ".join(stripped.split())
    return stripped or name


def sanitize_name(name, max_len=150):
    """Return a filesystem- and wikilink-safe name (bases, tables, notes, files)."""
    cleaned = []
    for c in unicodedata.normalize("NFC", str(name)):
        if c in LINK_DROP_CHARS:
            continue
        if c in LINK_DASH_CHARS:
            cleaned.append("-")
        elif c in INVALID_CHARS or c in "\r\n\t":
            cleaned.append(" ")
        else:
            cleaned.append(c)
    # Collapse runs of whitespace, then trim leading/trailing whitespace and dots
    # (trailing dots/spaces are hostile on Windows, leading dots hide files).
    cleaned = " ".join("".join(cleaned).split()).strip().strip(".").strip()
    return cleaned[:max_len].strip()


def property_name(name, strip_emojis=False):
    """Normalize a field name for frontmatter: trim (incl. NBSP), collapse spaces."""
    name = " ".join(str(name).replace("\u200b", "").replace("\ufeff", "").split())
    return strip_emoji(name) if strip_emojis else name


def _name_key(name):
    # Obsidian and macOS/Windows filesystems match names case-insensitively.
    return unicodedata.normalize("NFC", name).casefold()


def unique_name(used, desired, record_id):
    """Pick a collision-free name, appending the record id when needed.

    Deterministic across re-runs: the same record always resolves to the same
    name, so a second run overwrites rather than piling up new files.
    """
    name = desired or record_id
    if _name_key(name) not in used:
        used.add(_name_key(name))
        return name
    name = f"{name} ({record_id})"
    used.add(_name_key(name))
    return name


# --------------------------------------------------------------------------- #
# Attachments: downloaded once into a shared folder, referenced by filename
# --------------------------------------------------------------------------- #
def _is_attachment(value):
    return isinstance(value, dict) and "url" in value and "filename" in value


class Attachments:
    """Downloads attachments and hands out stable, unique, link-safe filenames.

    Files are named "<note name> - <original filename>", with a counter when two
    attachments would share a name. An existing file of the expected size is
    reused, so re-runs only download what is new or changed.
    """

    def __init__(self, folder, root):
        self.folder = Path(folder)
        self.root = root
        self.by_id = {}  # attachment id -> filename (lookups reuse the same file)
        self.used = set()
        self.stats = {"downloaded": 0, "reused": 0, "failed": 0}

    def filenames(self, value, note_name):
        atts = value if isinstance(value, list) else [value]
        return [self._filename(att, note_name) for att in atts if _is_attachment(att)]

    def _filename(self, att, note_name):
        att_id = att.get("id") or att["url"]
        if att_id in self.by_id:
            return self.by_id[att_id]

        original = Path(str(att.get("filename") or "attachment"))
        ext = original.suffix or mimetypes.guess_extension(att.get("type") or "") or ""
        # Sanitize without the dot: sanitize_name strips leading dots.
        ext = sanitize_name(ext.lstrip(".")).replace(" ", "")
        ext = f".{ext.lower()}" if ext else ""
        stem = sanitize_name(f"{note_name} - {original.stem}") or att_id
        filename, counter = f"{stem}{ext}", 2
        while _name_key(filename) in self.used:
            filename, counter = f"{stem} {counter}{ext}", counter + 1
        self.used.add(_name_key(filename))
        self.by_id[att_id] = filename

        self._download(att, _inside(self.root, self.folder / filename))
        return filename

    def _download(self, att, path):
        size = att.get("size")
        if path.exists() and (size is None or path.stat().st_size == size):
            self.stats["reused"] += 1
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(path.name + ".part")
        try:
            with urllib.request.urlopen(att["url"], timeout=60) as resp:
                part.write_bytes(resp.read())
            part.replace(path)
            self.stats["downloaded"] += 1
        except Exception as e:  # keep exporting; the embed stays and a re-run retries
            part.unlink(missing_ok=True)
            self.stats["failed"] += 1
            print(f"  ! Could not download attachment for {path.name}: {e}")


# --------------------------------------------------------------------------- #
# Field-value -> Markdown conversion (keyed on the schema field type)
# --------------------------------------------------------------------------- #
def _is_empty(value):
    return value is None or value == "" or value == [] or value == {}


def _render_generic(value):
    """Fallback rendering for scalars, lists and dicts of unknown shape."""
    if isinstance(value, list):
        return ", ".join(_render_generic(v) for v in value if not _is_empty(v))
    if isinstance(value, dict):
        # Prefer a human-friendly key if present, else a compact representation.
        for key in ("name", "text", "value", "label", "url"):
            if key in value:
                return str(value[key])
        return ", ".join(f"{k}: {v}" for k, v in value.items())
    return str(value)


def _collaborator_name(value):
    """Name only for a collaborator dict -- never the email address (PII)."""
    if isinstance(value, dict):
        return str(value.get("name") or "").strip()
    return str(value).strip()


def convert_field(field_type, value, id_to_title, attachment_names):
    """Convert one Airtable cell value to Markdown, or None to skip the field.

    `attachment_names` maps an attachment value to its downloaded filenames.
    """
    if _is_empty(value):
        return None

    if field_type == "multipleRecordLinks":
        ids = value if isinstance(value, list) else [value]
        links = [f"[[{id_to_title.get(rid, rid)}]]" for rid in ids]
        return " ".join(links) or None

    if field_type == "singleSelect":
        return str(value) or None

    if field_type == "multipleSelects":
        return ", ".join(str(v) for v in value if str(v)) or None

    if field_type == "multipleAttachments" or (
        isinstance(value, list) and value and all(_is_attachment(v) for v in value)
    ):
        return " ".join(f"![[{name}]]" for name in attachment_names(value)) or None

    if field_type == "singleCollaborator":
        return _collaborator_name(value) or None

    if field_type == "multipleCollaborators":
        names = [_collaborator_name(v) for v in value if _collaborator_name(v)]
        return ", ".join(names) or None

    if field_type == "barcode":
        return (value.get("text") if isinstance(value, dict) else str(value)) or None

    if field_type == "aiText":
        return (value.get("value") if isinstance(value, dict) else str(value)) or None

    # Scalars, dates, formulas, rollups, lookups, counts and anything else:
    # render generically. Warn once for types we've never seen so gaps surface.
    if field_type not in _warned_types and field_type in ("unknown",):
        _warned_types.add(field_type)
        print(f"  ! Unrecognized field type '{field_type}', rendering as text.")
    rendered = _render_generic(value)
    return rendered or None


# --------------------------------------------------------------------------- #
# Field-value -> YAML frontmatter property (typed, for Obsidian Bases)
# --------------------------------------------------------------------------- #
NUMERIC_TYPES = {
    "number", "percent", "currency", "rating", "duration", "autoNumber", "count",
}


def _parse_date(value):
    try:
        return datetime.date.fromisoformat(str(value))
    except ValueError:
        return None


def _parse_datetime(value):
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.datetime.fromisoformat(text)
    except ValueError:
        return None


def _scalarize(value):
    """Coerce a computed/unknown value to a YAML-friendly scalar."""
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    if isinstance(value, dict):
        return _render_generic(value)
    return str(value)


def field_to_property(field_type, value, id_to_title, attachment_names):
    """Convert a cell value to a typed Python value for YAML frontmatter.

    Returns None to omit the property (Obsidian treats a missing property as
    empty). Types are preserved so Bases can sort/filter numerically and by date,
    and wikilinks are emitted as strings that PyYAML quotes correctly.
    """
    if _is_empty(value):
        return None

    if field_type == "multipleRecordLinks":
        ids = value if isinstance(value, list) else [value]
        return [f"[[{id_to_title.get(rid, rid)}]]" for rid in ids] or None

    if field_type == "singleSelect":
        return str(value) or None

    if field_type == "multipleSelects":
        return [str(v) for v in value if str(v)] or None

    # Attachment fields, and lookups of attachment fields.
    if field_type == "multipleAttachments" or (
        isinstance(value, list) and value and all(_is_attachment(v) for v in value)
    ):
        return [f"[[{name}]]" for name in attachment_names(value)] or None

    if field_type == "singleCollaborator":
        return _collaborator_name(value) or None

    if field_type == "multipleCollaborators":
        return [n for n in (_collaborator_name(v) for v in value) if n] or None

    if field_type == "barcode":
        return (value.get("text") if isinstance(value, dict) else str(value)) or None

    if field_type == "aiText":
        return (value.get("value") if isinstance(value, dict) else str(value)) or None

    if field_type == "checkbox":
        return bool(value)

    if field_type in NUMERIC_TYPES:
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else _scalarize(value)

    if field_type == "date":
        return _parse_date(value) or str(value)

    if field_type in ("dateTime", "createdTime", "lastModifiedTime"):
        return _parse_datetime(value) or str(value)

    # formula / rollup / lookup / multipleLookupValues / anything else:
    # preserve list-ness and native scalar types where possible.
    if isinstance(value, list):
        return [_scalarize(v) for v in value if not _is_empty(v)] or None
    return _scalarize(value)


def build_frontmatter(record, table, id_to_title, attachment_names):
    """Build the YAML frontmatter block (including delimiters) for a record."""
    props = {}
    fields = record.get("fields", {})
    for name in table["field_order"]:
        field_type = table["field_types"][name]
        if field_type in SKIP_FIELD_TYPES or name not in fields or name == table["body_field"]:
            continue
        prop = field_to_property(field_type, fields[name], id_to_title, attachment_names)
        if prop is None or prop == "" or prop == []:
            continue
        if name in table["float_fields"] and type(prop) is int:
            prop = float(prop)
        props[table["props"][name]] = prop
    # Always emit the block, even when empty, so every note has frontmatter.
    dumped = yaml.safe_dump(
        props, allow_unicode=True, sort_keys=False, default_flow_style=False
    ) if props else ""
    return f"---\n{dumped}---\n\n"


# --------------------------------------------------------------------------- #
# Export orchestration
# --------------------------------------------------------------------------- #
def _primary_field_name(table_schema):
    """Resolve a table's primary field name from its schema."""
    for field in table_schema.fields:
        if field.id == table_schema.primary_field_id:
            return field.name
    # Fallback: first field (Airtable guarantees a primary, so this is defensive).
    return table_schema.fields[0].name if table_schema.fields else None


def _primary_value_text(value):
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join(_render_generic(v) for v in value)
    return _render_generic(value)


def _property_names(field_order, strip_emojis):
    """Map each field name to a normalized, unique frontmatter property name."""
    props, used = {}, set()
    for name in field_order:
        prop = base = property_name(name, strip_emojis) or name
        counter = 2
        while _name_key(prop) in used:
            prop, counter = f"{base} {counter}", counter + 1
        used.add(_name_key(prop))
        props[name] = prop
    return props


def discover(api, config):
    """Pass 1: walk every base/table, cache records, build the recId->title map.

    Note names are made unique across the whole export (Obsidian resolves
    [[links]] by name, regardless of folder), and the same names are used both
    for filenames and as link targets so every link resolves.

    Returns (tables, id_to_title, stats) where `tables` is a list of dicts
    holding everything pass 2 needs, so records are fetched only once.
    """
    tables = []
    id_to_title = {}
    used_notes = set()
    stats = {"bases": 0, "tables": 0, "records": 0, "skipped_bases": 0}

    for base in api.bases():
        if getattr(base, "permission_level", None) == "none":
            print(f"Skipping base {base.id} (no access).")
            stats["skipped_bases"] += 1
            continue
        try:
            base_name = base.name
            base_tables = base.tables()
        except Exception as e:  # 403 / scope / network issues for a whole base
            print(f"Skipping base {base.id}: {e}")
            stats["skipped_bases"] += 1
            continue

        stats["bases"] += 1
        print(f"Base: {base_name}")
        for table in base_tables:
            ts = table.schema()
            primary = _primary_field_name(ts)
            field_types = {f.name: f.type for f in ts.fields}
            field_order = [f.name for f in ts.fields]
            # Numbers with decimal places: Airtable returns whole values as ints
            # (12 vs 12.5), so these are written as floats for a consistent type.
            float_fields = {
                f.name for f in ts.fields
                if f.type in ("number", "currency", "percent")
                and getattr(getattr(f, "options", None), "precision", 0)
            }
            props = _property_names(field_order, config["strip_emoji"])
            body = config["body"]
            body_field = None
            if body not in (BODY_FIELDS, BODY_NONE):
                body_field = next(
                    (n for n in field_order if body in (n, props[n])), None
                )

            print(f"  Reading table: {ts.name}")
            records = table.all()

            for rec in records:
                value = rec.get("fields", {}).get(primary)
                title = sanitize_name(_primary_value_text(value))
                id_to_title[rec["id"]] = unique_name(used_notes, title, rec["id"])

            tables.append(
                {
                    "base_id": base.id,
                    "base_name": base_name,
                    "table_name": ts.name,
                    "primary": primary,
                    "field_types": field_types,
                    "field_order": field_order,
                    "float_fields": float_fields,
                    "props": props,
                    "body_field": body_field,
                    "records": records,
                }
            )
            stats["tables"] += 1
            stats["records"] += len(records)

    return tables, id_to_title, stats


def record_markdown(record, table, id_to_title, attachment_names, body):
    """Assemble the Markdown body for a single record according to A2O_BODY."""
    fields = record.get("fields", {})
    if body == BODY_NONE:
        return ""
    if body != BODY_FIELDS:
        name = table["body_field"]
        if name is None or name not in fields:
            return ""
        content = convert_field(table["field_types"][name], fields[name], id_to_title, attachment_names)
        return f"{content}\n" if content else ""
    blocks = []
    for name in table["field_order"]:
        field_type = table["field_types"][name]
        if field_type in SKIP_FIELD_TYPES or name not in fields:
            continue
        content = convert_field(field_type, fields[name], id_to_title, attachment_names)
        if content:
            blocks.append(f"## {table['props'][name]}\n{content}\n")
    return "\n".join(blocks)


def _inside(root, path):
    """Return `path` if it resolves inside `root`, else refuse to write there."""
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise SystemExit(f"Refusing to write outside '{root}': {resolved}")
    return path


def write(tables, id_to_title, config):
    """Pass 2: write one Markdown file per record from the cached data.

    Everything is written inside the output directory; nothing is deleted.
    """
    output_dir = Path(config["output_dir"])
    root = output_dir.resolve()
    attachments = Attachments(config["attachments_dir"], root)
    used_base_dirs = set()
    if attachments.folder.resolve().parent == root:
        # Don't let a base folder share a name with the attachments folder.
        used_base_dirs.add(_name_key(attachments.folder.name))
    base_dirs = {}  # base_id -> Path (so tables under the same base share a folder)
    files_written = 0

    for table in tables:
        if not table["records"]:
            print(f"  (skipping empty table {table['base_name']}/{table['table_name']})")
            continue

        # Key and disambiguate by base id: names are user-controlled and may
        # collide or sanitize to nothing, and must never reach the path unsanitized.
        base_id = table["base_id"]
        if base_id not in base_dirs:
            base_name = table["base_name"]
            if config["strip_emoji"]:
                base_name = strip_emoji(base_name)
            folder = unique_name(used_base_dirs, sanitize_name(base_name), base_id)
            base_dirs[base_id] = output_dir / folder
        base_dir = base_dirs[base_id]

        table_name = table["table_name"]
        if config["strip_emoji"]:
            table_name = strip_emoji(table_name)
        table_dir = _inside(root, base_dir / (sanitize_name(table_name) or "table"))
        table_dir.mkdir(parents=True, exist_ok=True)

        for rec in table["records"]:
            # Already unique across the whole export (see discover()).
            filename = id_to_title[rec["id"]]

            def attachment_names(value, note=filename):
                return attachments.filenames(value, note)

            frontmatter = build_frontmatter(rec, table, id_to_title, attachment_names)
            body = record_markdown(rec, table, id_to_title, attachment_names, config["body"])
            # 'w' via write_text: re-runs refresh content instead of duplicating it.
            _inside(root, table_dir / f"{filename}.md").write_text(frontmatter + body, encoding="utf-8")
            files_written += 1

    return files_written, attachments.stats


def _env_flag(name):
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def main():
    load_dotenv()

    pat = os.getenv("AIRTABLE_PAT")
    if not pat:
        raise SystemExit(
            "Missing required setting: AIRTABLE_PAT. "
            "Copy .env.sample to .env and fill in your value."
        )
    output_dir = os.getenv("A2O_OUTPUT_DIR") or "export"
    config = {
        "output_dir": output_dir,
        "attachments_dir": os.getenv("A2O_ATTACHMENTS_DIR") or str(Path(output_dir) / "_attachments"),
        "body": (os.getenv("A2O_BODY") or BODY_NONE).strip(),
        "strip_emoji": _env_flag("A2O_STRIP_EMOJI"),
    }
    if config["body"].lower() in (BODY_FIELDS, BODY_NONE):
        config["body"] = config["body"].lower()
    # Fail before touching the network if attachments would land outside the export.
    _inside(Path(output_dir).resolve(), Path(config["attachments_dir"]))

    api = Api(pat)
    try:
        tables, id_to_title, stats = discover(api, config)
    except Exception as e:
        # Most commonly an invalid/expired token or a missing schema.bases:read scope.
        raise SystemExit(
            f"Failed to read from Airtable: {e}\n"
            "Check that AIRTABLE_PAT is valid and has the scopes "
            "'schema.bases:read' and 'data.records:read'."
        )

    if config["body"] not in (BODY_FIELDS, BODY_NONE) and not any(t["body_field"] for t in tables):
        print(f"  ! A2O_BODY field '{config['body']}' was not found in any table; bodies will be empty.")

    files_written, att_stats = write(tables, id_to_title, config)

    print(
        "\nDone. "
        f"{stats['bases']} base(s), {stats['tables']} table(s), "
        f"{stats['records']} record(s) -> {files_written} file(s) in '{output_dir}/'."
        + (f" Skipped {stats['skipped_bases']} base(s)." if stats["skipped_bases"] else "")
        + f"\nAttachments in '{config['attachments_dir']}/': {att_stats['downloaded']} downloaded, "
        f"{att_stats['reused']} already present"
        + (f", {att_stats['failed']} FAILED (re-run to retry)." if att_stats["failed"] else ".")
    )


if __name__ == "__main__":
    main()
