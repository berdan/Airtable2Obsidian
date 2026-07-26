"""Export every accessible Airtable base into Obsidian-ready Markdown.

Discovers all bases and tables via the Airtable metadata API (using pyairtable),
reads each table's schema so the primary field and field types are known, and
writes one Markdown file per record laid out as:

    <output_dir>/<base name>/<table name>/<record>.md

Linked records become [[wikilinks]] (resolved to the linked record's primary
field) and select fields become #tags, so the export is Obsidian-ready.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from pyairtable import Api

# Characters that are not allowed in file/folder names on common filesystems.
INVALID_CHARS = set('\\/:*?"<>|')

# Field types whose values we intentionally do not export.
#   button / externalSyncSource : not real content
#   createdBy / lastModifiedBy  : collaborator audit fields (contain emails -> PII)
SKIP_FIELD_TYPES = {"button", "externalSyncSource", "createdBy", "lastModifiedBy"}

# Remember unknown field types we've warned about so we only warn once each.
_warned_types = set()


# --------------------------------------------------------------------------- #
# Sanitization helpers
# --------------------------------------------------------------------------- #
def sanitize_name(name, max_len=200):
    """Return a filesystem-safe name (for bases, tables and record titles)."""
    cleaned = "".join(" " if c in INVALID_CHARS or c in "\r\n\t" else c for c in str(name))
    # Collapse runs of whitespace, then trim leading/trailing whitespace and dots
    # (trailing dots/spaces are hostile on Windows).
    cleaned = " ".join(cleaned.split()).strip().strip(".").strip()
    return cleaned[:max_len].strip()


def sanitize_tag(value):
    """Turn a select-option string into an Obsidian tag body (no leading #)."""
    tag = str(value).strip().lstrip("#")
    tag = "".join("_" if c in INVALID_CHARS or c.isspace() else c for c in tag)
    return "_".join(filter(None, tag.split("_")))


def unique_name(used, desired, record_id):
    """Pick a collision-free name, appending the record id when needed.

    Deterministic across re-runs: the same record always resolves to the same
    name, so a second run overwrites rather than piling up new files.
    """
    name = desired or record_id
    if name not in used:
        used.add(name)
        return name
    name = f"{name} ({record_id})"
    used.add(name)
    return name


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


def convert_field(field_type, value, id_to_title):
    """Convert one Airtable cell value to Markdown, or None to skip the field."""
    if _is_empty(value):
        return None

    if field_type == "multipleRecordLinks":
        ids = value if isinstance(value, list) else [value]
        links = [f"[[{id_to_title.get(rid, rid)}]]" for rid in ids]
        return " ".join(links) or None

    if field_type == "singleSelect":
        tag = sanitize_tag(value)
        return f"#{tag}" if tag else None

    if field_type == "multipleSelects":
        tags = [f"#{sanitize_tag(v)}" for v in value if sanitize_tag(v)]
        return " ".join(tags) or None

    if field_type == "multipleAttachments":
        links = []
        for att in value if isinstance(value, list) else [value]:
            if isinstance(att, dict) and att.get("url"):
                links.append(f"[{att.get('filename', 'attachment')}]({att['url']})")
        return " ".join(links) or None

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


def discover(api):
    """Pass 1: walk every base/table, cache records, build the recId->title map.

    Returns (tables, id_to_title, stats) where `tables` is a list of dicts
    holding everything pass 2 needs, so records are fetched only once.
    """
    tables = []
    id_to_title = {}
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

            print(f"  Reading table: {ts.name}")
            records = table.all()

            for rec in records:
                value = rec.get("fields", {}).get(primary)
                title = sanitize_name(_primary_value_text(value)) or rec["id"]
                id_to_title[rec["id"]] = title

            tables.append(
                {
                    "base_name": base_name,
                    "table_name": ts.name,
                    "primary": primary,
                    "field_types": field_types,
                    "field_order": field_order,
                    "records": records,
                }
            )
            stats["tables"] += 1
            stats["records"] += len(records)

    return tables, id_to_title, stats


def record_markdown(record, table, id_to_title):
    """Assemble the full Markdown body for a single record."""
    fields = record.get("fields", {})
    blocks = []
    for name in table["field_order"]:
        field_type = table["field_types"][name]
        if field_type in SKIP_FIELD_TYPES or name not in fields:
            continue
        content = convert_field(field_type, fields[name], id_to_title)
        if content:
            blocks.append(f"## {name}\n{content}\n")
    return "\n".join(blocks)


def write(tables, id_to_title, output_dir):
    """Pass 2: write one Markdown file per record from the cached data."""
    output_dir = Path(output_dir)
    used_base_dirs = set()
    base_dirs = {}  # base_name -> Path (so tables under the same base share a folder)
    files_written = 0

    for table in tables:
        if not table["records"]:
            print(f"  (skipping empty table {table['base_name']}/{table['table_name']})")
            continue

        base_name = table["base_name"]
        if base_name not in base_dirs:
            folder = unique_name(used_base_dirs, sanitize_name(base_name), base_name)
            base_dirs[base_name] = output_dir / folder
        base_dir = base_dirs[base_name]

        table_dir = base_dir / (sanitize_name(table["table_name"]) or "table")
        table_dir.mkdir(parents=True, exist_ok=True)

        used_files = set()
        for rec in table["records"]:
            title = id_to_title.get(rec["id"], rec["id"])
            filename = unique_name(used_files, title, rec["id"])
            body = record_markdown(rec, table, id_to_title)
            # 'w' via write_text: re-runs refresh content instead of duplicating it.
            (table_dir / f"{filename}.md").write_text(body, encoding="utf-8")
            files_written += 1

    return files_written


def main():
    load_dotenv()

    pat = os.getenv("AIRTABLE_PAT")
    if not pat:
        raise SystemExit(
            "Missing required setting: AIRTABLE_PAT. "
            "Copy .env.sample to .env and fill in your value."
        )
    output_dir = os.getenv("A2O_OUTPUT_DIR", "export")

    api = Api(pat)
    try:
        tables, id_to_title, stats = discover(api)
    except Exception as e:
        # Most commonly an invalid/expired token or a missing schema.bases:read scope.
        raise SystemExit(
            f"Failed to read from Airtable: {e}\n"
            "Check that AIRTABLE_PAT is valid and has the scopes "
            "'schema.bases:read' and 'data.records:read'."
        )

    files_written = write(tables, id_to_title, output_dir)

    print(
        "\nDone. "
        f"{stats['bases']} base(s), {stats['tables']} table(s), "
        f"{stats['records']} record(s) -> {files_written} file(s) in '{output_dir}/'."
        + (f" Skipped {stats['skipped_bases']} base(s)." if stats["skipped_bases"] else "")
    )


if __name__ == "__main__":
    main()
