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
- Using the field types from the schema, it translates Airtable select fields to Obsidian **#tags** and links between records to Obsidian **[[wikilinks]]**, so your notes stay linked and tagged.

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
