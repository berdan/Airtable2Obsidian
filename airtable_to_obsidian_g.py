import requests
import pandas as pd
import os
from dotenv import load_dotenv

class AirtableExporter:
    def __init__(self, api_key, base_id, table_names):
        self.api_key = api_key
        self.base_id = base_id
        self.table_names = table_names
        self.id_to_title = {}
        self.headers = {
            'Authorization': f'Bearer {self.api_key}',
        }

    def build_id_to_title_mapping(self):
        global id_to_title
        # Iterate over each table to build the ID to title mapping
        for i in self.table_names:
            print(f'Building dictionary for {i}...')
            url = f'https://api.airtable.com/v0/{self.base_id}/{i}'

            offset = None
            while True:
                if offset:
                    response = requests.get(url, headers=self.headers, params={'offset': offset}, timeout=10)
                else:
                    response = requests.get(url, headers=self.headers, timeout=10)

                if response.status_code != 200:
                    print(f"Error fetching data for {i}: {response.status_code} - {response.text}")
                    break

                try:
                    data = response.json()
                    df = pd.json_normalize(data['records'])

                    # Build the ID to title mapping for the current table
                    for index, row in df.iterrows():
                        record_id = row['id']
                        title = row['fields.Title'][:200]  # This code assumes your first field is called Title, adjust if not the case

                        if title is None:
                            print(f"Skipping record {record_id} because the field Title is not filled.")
                            continue
                        # Ensure that the ID and title are strings
                        if isinstance(record_id, str) and isinstance(title, str):
                            # Sanitize the title to create a valid filename
                            safe_title = ''.join(c if c not in ('\\', '/', ':', '*', '?', '"', '<', '>', '|') else ' ' for c in title).replace('\n', ' ')
                            self.id_to_title[record_id] = safe_title
                        else:
                            # Convert to string if not already a string
                            record_id_str = str(record_id) if not isinstance(record_id, str) else record_id
                            title_str = str(title) if not isinstance(title, str) else title

                            # Sanitize the title to create a valid filename
                            safe_title = ''.join(c if c not in ('\\', '/', ':', '*', '?', '"', '<', '>', '|') else ' ' for c in title_str).replace('\n', ' ')
                            self.id_to_title[record_id_str] = safe_title

                    offset = data.get('offset')
                    if not offset:
                        break
                except KeyError:
                    print(f"No records found for {i}.")
                    break
                except Exception as e:
                    print(f"Error processing data for {i}: {e}")
                    break

        # After building the id_to_title mapping, write it to a text file
        with open('id_to_title.txt', 'w', encoding='utf-8') as f:
            for record_id, title in self.id_to_title.items():
                f.write(f"{record_id}: {title}\n")

    def load_id_to_title_mapping(self):
        global id_to_title
        try:
            with open('id_to_title.txt', 'r', encoding='utf-8') as f:
                for line in f:
                    record_id, title = line.strip().split(': ', 1)
                    self.id_to_title[record_id] = title
        except Exception as e:
            print(f"Error loading ID to title mapping: {e}")

    def process_tables(self):
        # Check if the id_to_title.txt file exists, and load it if it does, otherwise build it
        if os.path.exists('id_to_title.txt'):
            self.load_id_to_title_mapping()
        else:
            print(f'No dictionary found.')
            self.build_id_to_title_mapping()

        # Now process each table to create markdown files
        for i in self.table_names:
            print(f"Converting records in table {i} to Markdown")
            url = f'https://api.airtable.com/v0/{self.base_id}/{i}'

            # Create a directory for the current table if it doesn't exist
            os.makedirs(i, exist_ok=True)

            offset = None
            while True:
                try:
                    if offset:
                        response = requests.get(url, headers=self.headers, params={'offset': offset}, timeout=10)
                    else:
                        response = requests.get(url, headers=self.headers, timeout=10)

                    if response.status_code != 200:
                        print(f"Error fetching data for {i}: {response.status_code} - {response.text}")
                        break

                    data = response.json()
                    df = pd.json_normalize(data['records'])

                    # Process each row to create markdown files, and make them Obsidian ready
                    for index, row in df.iterrows():
                        try:
                            # Replace IDs in the row with corresponding titles using the global id_to_title mapping
                            for col in df.columns:
                                content = str(row[col])
                                if content.startswith("['rec"):
                                    new_content = []
                                    for item in row[col]:
                                        item = self.id_to_title.get(item, item)
                                        new_content.append(item)
                                    content = str(new_content)
                                    content = content.replace('\']', ']]').replace('[\'', '[[').replace('\', \'', ']] [[')

                                elif content.startswith('rec'):
                                    content = self.id_to_title.get(content, content)
                                    content = content.replace('\']', ']]').replace('[\'', '[[').replace('\',', ']]')

                                # Post-processing for content that starts with "['"
                                elif content.startswith("['"):
                                    content = content.replace("[\'", "#")
                                    if content.startswith("#"):
                                        content = content.replace(" ", "_").replace("\',_\'", " #").replace("\']", "")
                                # Sanitize the title to create a valid filename
                                safe_title = ''.join(c if c not in ('\\', '/', ':', '*', '?', '"', '<', '>', '|') else ' ' for c in row['fields.Title'][:200]).replace('\n', ' ')
                                filename = os.path.join(i, f"{safe_title}.md")

                                # Write the content to the markdown file without any changes
                                with open(filename, 'a', encoding='utf-8') as f:  # Specify UTF-8 encoding
                                    if col not in ['id', 'createdTime', 'fields.lastModified', 'fields.Created By.id', 'fields.Created By.email', 'fields.Created By.name']:
                                        col = col.replace('fields.', '')
                                        f.write(f"## {col}\n{content}\n\n")  # Write column title and original value

                        except Exception as e:
                            print(f"Error processing record for {i}: {e} on {row['fields.Title']}")
                            continue

                    # Check for the presence of an offset in the response to continue fetching
                    offset = data.get('offset')
                    if not offset:  # Break the loop if no more records are available
                        break
                except KeyError:
                    print(f"No records found for {i}.")
                    break  # Exit the loop if no records are found
                except Exception as e:
                    print(f"Error processing data for {i}: {e}")
                    break  # Exit the loop on any other exception

# Load configuration from the .env file
load_dotenv()

API_KEY = os.getenv('AIRTABLE_PAT')  # Use the personal access token from the .env file 
BASE_ID = os.getenv('AIRTABLE_BASE_ID')
# TABLE_NAMES is a comma-separated list in the .env file, e.g. Table1,Table2,Table3
TABLE_NAMES = [name.strip() for name in os.getenv('AIRTABLE_TABLE_NAMES', '').split(',') if name.strip()]

# Fail early with a clear message if any required setting is missing
missing = [
    var for var, value in (
        ('AIRTABLE_PAT', API_KEY),
        ('AIRTABLE_BASE_ID', BASE_ID),
        ('AIRTABLE_TABLE_NAMES', TABLE_NAMES),
    ) if not value
]
if missing:
    raise SystemExit(
        f"Missing required setting(s): {', '.join(missing)}. "
        "Copy .env.sample to .env and fill in your values."
    )

# Initialize the exporter
exporter = AirtableExporter(API_KEY, BASE_ID, TABLE_NAMES)
exporter.process_tables()