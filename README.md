# Airtable2Obsidian
A simple Python script that takes all records from your Airtable, and transforms them into Obsidian ready Markdown files. 

![picture](airtable2obsidian.png)

## Why?
Airtable2Obsidian does not just transform Airtable records into simple Markdown, but ensures that tags and links between Airtable records within and across tables are kept, so that when using Obsidian Markdown files are linked and tagged!

## Usage
The script works like this:

- It **automatically discovers every base and table** your token can access — no need to list them.
- Each base becomes a top-level folder, each table a sub-folder, and each record a separate Markdown file: `export/<base>/<table>/<record>.md`
- It reads each table's schema, so the note title comes from the table's actual **primary field** (no assumptions about a "Title" column).
- Every field is written as a typed **YAML frontmatter property**, so the export works directly with [Obsidian Bases](https://help.obsidian.md/bases). Numbers, checkboxes and dates keep their real types (so Bases can sort/filter them), multi-value fields become lists, and linked records become `[[wikilink]]` list properties.
- **Attachments are downloaded** into one shared folder (`export/_attachments` by default), because Airtable's attachment URLs expire after a few hours. Each file is named `<note name> - <original filename>` (with a counter if two would clash) and attachment properties become lists of `[[filename.ext]]` embeds. Files that already exist are reused, so re-runs only download what's new.
- **Note names are wikilink-safe and unique across the whole export.** Obsidian can't link to names containing `# ^ [ ] | \ / :`, so `#` is dropped and the others become `-` (e.g. `Platinum #3776` → `Platinum 3776.md`). If two records end up with the same name, the record id is appended. Links use the same names, so they always resolve; the original value stays in the primary-field property.
- **Property names are cleaned up**: leading/trailing whitespace (including non-breaking spaces) is trimmed. Emoji can optionally be stripped from property and folder names (`Pennor ✒️` → `Pennor`).
- **The note body is configurable**: frontmatter only (default), every field repeated as `## Field` sections, or a long-text field of your choice as the body.
- The script **only writes inside the output directory** and never deletes anything. Point `A2O_OUTPUT_DIR` at a new folder (e.g. `export-2`) to compare a fresh export with an old one.

Example note (with the default `A2O_BODY=none`):

```markdown
---
Name: Canon RF 50mm
Price: 649
Discontinued: false
Purchased: 2023-05-01
Mount:
  - "[[RF Mount]]"
Categories:
  - Prime
  - Portrait
Photo:
  - "[[Canon RF 50mm - IMG_1234.jpg]]"
---
```

### Configuration

Configuration lives in a `.env` file (it is not committed to the repo). Copy the provided sample and fill in your token:

```
cp .env.sample .env
```

Then edit `.env`:

```
AIRTABLE_PAT=your_personal_access_token_here
# Optional, defaults to ./export
# A2O_OUTPUT_DIR=export
```

Optional settings (all documented in `.env.sample`):

| Setting | Default | What it does |
| --- | --- | --- |
| `A2O_OUTPUT_DIR` | `export` | Where the notes are written. |
| `A2O_ATTACHMENTS_DIR` | `<A2O_OUTPUT_DIR>/_attachments` | Shared folder for downloaded attachments. Must be inside the output directory. |
| `A2O_BODY` | `none` | `none` = frontmatter only, `fields` = every field as `## Field` + value, or the name of a long-text field to use as the body (it is then left out of the frontmatter). |
| `A2O_STRIP_EMOJI` | `false` | Strip emoji from property and folder names. |

Any setting can also be given on the command line for a single run, e.g. `A2O_OUTPUT_DIR=export-2 uv run airtable_to_obsidian_g.py`.

Create a personal access token at https://airtable.com/create/tokens. It needs these scopes:

- `schema.bases:read` — list bases and read table/field schema
- `data.records:read` — read record data

Grant the token access to the bases you want to export.

### Install & run

This project uses [uv](https://docs.astral.sh/uv/). Once uv is installed, it will handle the virtual environment and dependencies for you:

```
uv run airtable_to_obsidian_g.py
```

`uv run` automatically creates the virtual environment and installs the locked dependencies on first use. To set up the environment without running the script, use `uv sync`.

## Note
> [!WARNING]
> This project was made for my own use and works fine for my own use. It is no guarantee it might work for you, but it might help and save you time in figuring out your own solution.
