import base64
import csv
import io
import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone

import functions_framework
import google.cloud.logging
from google.auth import default as get_credentials
from google.cloud import bigquery, firestore, secretmanager, storage
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

# --- Configuration ---
with open('config.json', 'r') as f:
    config = json.load(f)

PROJECT_ID = config.get('project_id')
SECRET_ID = config.get('secret_id')
BUCKET_NAME = config.get('bucket_name')
LOOKBACK_MINUTES = config.get('lookback_minutes')
FOLDER_IDS = config.get('folder_ids', {})
PARSERS = config.get('parsers', {})
DRIVE_SCOPES = ['https://www.googleapis.com/auth/drive']
FIRESTORE_COLLECTION = config.get('firestore_collection')
# ---------------------


# Initialize clients globally to reuse connections across function invocations
try:
    # Set up Google Cloud Logging
    logging_client = google.cloud.logging.Client(project=PROJECT_ID)
    logging_client.setup_logging()

    CREDENTIALS, _ = get_credentials(scopes=DRIVE_SCOPES)
    DRIVE_SERVICE = build('drive', 'v3', credentials=CREDENTIALS)
    STORAGE_CLIENT = storage.Client(project=PROJECT_ID)
    BIGQUERY_CLIENT = bigquery.Client(project=PROJECT_ID)
    FIRESTORE_CLIENT = firestore.Client(project=PROJECT_ID)
    SECRET_MANAGER_CLIENT = secretmanager.SecretManagerServiceClient()
    
except Exception as e:
    logging.error(f"Failed to initialize global clients: {e}")
    DRIVE_SERVICE = None
    STORAGE_CLIENT = None
    BIGQUERY_CLIENT = None
    FIRESTORE_CLIENT = None
    SECRET_MANAGER_CLIENT = None

# --- Global cache for the secret ---
_webhook_secret = None

def _get_secret(secret_id, project_id, version_id="latest"):
    """Retrieves a secret from Google Cloud Secret Manager."""
    global _webhook_secret
    if _webhook_secret:
        return _webhook_secret

    if not SECRET_MANAGER_CLIENT:
        logging.error("Secret Manager client not initialized.")
        raise ConnectionError("Secret Manager client not available")

    name = f"projects/{project_id}/secrets/{secret_id}/versions/{version_id}"
    try:
        response = SECRET_MANAGER_CLIENT.access_secret_version(request={"name": name})
        _webhook_secret = response.payload.data.decode("UTF-8")
        logging.info("Successfully retrieved and cached webhook secret.")
        return _webhook_secret
    except Exception as e:
        logging.error(f"Failed to access secret version '{name}': {e}")
        raise

# MimeType mapping for Google Workspace files to their export formats
EXPORT_MIMETYPES = {
    'application/vnd.google-apps.document': {
        'mimeType': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'extension': '.docx'
    },
    'application/vnd.google-apps.spreadsheet': {
        'mimeType': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'extension': '.xlsx'
    },
    'application/vnd.google-apps.presentation': {
        'mimeType': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
        'extension': '.pptx'
    },
}


# ==============================================================================
# === GOOGLE DRIVE WATCH SETUP (PUB/SUB TRIGGER) ===============================
# ==============================================================================

@functions_framework.cloud_event
def drive_setup_watch(cloud_event):
    """
    Triggered from a message on a Cloud Pub/Sub topic to set up a Drive watch channel.
    """
    if not FIRESTORE_CLIENT:
        logging.error("Firestore client not initialized. Exiting function.")
        return "Internal Server Error: Firestore client not initialized", 500

    try:
        message_data = base64.b64decode(cloud_event.data["message"]["data"]).decode('utf-8')
        data = json.loads(message_data)
        folder_key = data.get('folder_key')
    except (KeyError, json.JSONDecodeError, TypeError) as e:
        logging.error(f"Failed to decode Pub/Sub message: {e}")
        return "Bad Request: Invalid Pub/Sub message format", 400

    if not folder_key:
        logging.error("'folder_key' not found in Pub/Sub message.")
        return "Bad Request: 'folder_key' is required", 400

    folder_id = FOLDER_IDS.get(folder_key)
    if not folder_id:
        logging.error(f"Configuration for folder_key '{folder_key}' not found in config.json.")
        return f"Bad Request: Configuration for '{folder_key}' not found", 400
    function_url_base = data.get('function_url_base') or os.environ.get('FUNCTION_URL_BASE')

    if not all([folder_id, function_url_base]):
        error_msg = "'folder_id' not found in config or 'FUNCTION_URL_BASE' not found in env vars or message."
        logging.error(error_msg)
        return "Bad Request: Configuration incomplete", 400

    try:
        secret = _get_secret(SECRET_ID, PROJECT_ID)
        function_url = f"{function_url_base}/{secret}"
    except Exception as e:
        logging.error(f"Failed to retrieve webhook secret: {e}")
        return "Internal Server Error: Could not get secret", 500

    logging.info(f"--- Setting up watch for: {folder_key} ({folder_id}) ---")
    
    # Pass the folder_key to the helper function to manage Firestore state
    resource_id, channel_id = _setup_drive_watch_channel(folder_id, function_url, folder_key)

    if resource_id and channel_id:
        # The helper function now manages Firestore persistence
        return "Channel setup successful", 200
    else:
        logging.error(f"Failed to set up watch channel for '{folder_key}'.")
        return "Internal Server Error", 500


def _setup_drive_watch_channel(folder_id, function_url, folder_key):
    """
    Helper function to create a push notification channel.
    It stops any existing channel for the folder before creating a new one.
    """
    if not DRIVE_SERVICE or not FIRESTORE_CLIENT:
        logging.error("Drive service or Firestore client not initialized.")
        return None, None

    # --- 1. Stop Existing Channel (if any) ---
    try:
        doc_ref = FIRESTORE_CLIENT.collection(FIRESTORE_COLLECTION).document(folder_key)
        doc = doc_ref.get()
        if doc.exists:
            channel_info = doc.to_dict()
            channel_id = channel_info.get('channel_id')
            resource_id = channel_info.get('resource_id')
            if channel_id and resource_id:
                logging.info(f"Stopping existing watch channel '{channel_id}' for folder '{folder_key}'.")
                stop_request_body = {'id': channel_id, 'resourceId': resource_id}
                DRIVE_SERVICE.channels().stop(body=stop_request_body).execute()
    except HttpError as e:
        # A 404 Not Found error is expected if the channel has already expired.
        if e.resp.status == 404:
            logging.warning(f"Watch channel for '{folder_key}' did not exist or already expired. Proceeding to create a new one.")
        else:
            logging.error(f"An HTTP error occurred while stopping the old channel: {e}")
            # Decide if you want to proceed or fail here. Proceeding is often safe.
    except Exception as e:
        logging.error(f"An unexpected error occurred while stopping the old channel: {e}", exc_info=True)
        # Decide if you want to proceed or fail.

    # --- 2. Create New Channel ---
    try:
        # Google Drive API watch notifications have a maximum lifespan of 7 days.
        expiration_date = datetime.now(timezone.utc) + timedelta(days=7)
        
        channel_request_body = {
            'id': str(uuid.uuid4()),
            'type': 'web_hook',
            'address': function_url,
            'expiration': int(expiration_date.timestamp() * 1000)
        }

        logging.info(f"Attempting to create a new watch on folder: {folder_id}")
        response = DRIVE_SERVICE.files().watch(
            fileId=folder_id,
            body=channel_request_body,
            supportsAllDrives=True
        ).execute()

        new_channel_id = response.get('id')
        new_resource_id = response.get('resourceId')
        logging.info(f"Successfully set up new notification channel: {new_channel_id}")

        # --- 3. Persist New Channel Details ---
        channel_data = {
            'channel_id': new_channel_id,
            'resource_id': new_resource_id,
            'folder_id': folder_id,
            'last_updated': firestore.SERVER_TIMESTAMP
        }
        doc_ref.set(channel_data)
        logging.info(f"Successfully stored new channel details in Firestore for '{folder_key}'.")

        return new_resource_id, new_channel_id

    except HttpError as error:
        logging.error(f"An HTTP error occurred during new watch setup: {error}")
    except Exception as e:
        logging.error(f"An unexpected error occurred during new watch setup: {e}", exc_info=True)
    
    return None, None




# ==============================================================================
# === GOOGLE DRIVE FILE DOWNLOADER (HTTP TRIGGER) ==============================
# ==============================================================================

@functions_framework.http
def drive_file_downloader(request):
    """
    An HTTP Cloud Function triggered by Google Drive Push Notifications on a FOLDER.
    It finds recent files in that folder, downloads/exports them, and uploads to GCS.
    """
    if not DRIVE_SERVICE or not STORAGE_CLIENT:
        logging.error("Clients are not initialized. Exiting function.")
        return "Internal Server Error: Clients not initialized", 500

    # --- 1. Validate Webhook Secret and Process Notification ---
    try:
        secret = _get_secret(SECRET_ID, PROJECT_ID)
        expected_path_suffix = f"/{secret}"
        if not request.path.endswith(expected_path_suffix):
            logging.warning("Webhook called with invalid secret.")
            return "Forbidden", 403
    except Exception as e:
        logging.error(f"Failed to validate webhook secret: {e}")
        return "Internal Server Error", 500

    resource_state = request.headers.get('X-Goog-Resource-State')
    resource_id = request.headers.get('X-Goog-Resource-ID')
    
    logging.info(f"Received notification: state={resource_state}, resource_id={resource_id}")

    if resource_state not in ('update', 'add'):
        logging.info(f"Ignoring notification with state: {resource_state}")
        return "Notification ignored", 204

    if not resource_id:
        logging.error("No resource ID (X-Goog-Resource-ID) found in headers.")
        return "Bad Request: Missing resource ID", 400

    # Look up the folder_id using the resource_id
    # --- 2. Look up folder_key and folder_id from Firestore ---
    # Query Firestore to find the document matching the resource_id from the notification.
    # This is more reliable than a static mapping, as channels are dynamic.
    docs = FIRESTORE_CLIENT.collection(FIRESTORE_COLLECTION).where('resource_id', '==', resource_id).limit(1).stream()
    
    folder_key = None
    folder_id = None
    
    # There should only be one document, but we loop just in case.
    for doc in docs:
        folder_key = doc.id
        folder_data = doc.to_dict()
        folder_id = folder_data.get('folder_id')
        break # Exit after finding the first match

    if not folder_key or not folder_id:
        logging.error(f"No matching folder configuration found in Firestore for resource_id '{resource_id}'.")
        return "Bad Request: Unknown resource ID", 400

    logging.info(f"Matched resource ID '{resource_id}' to folder '{folder_key}' (ID: {folder_id})")

    try:
        # --- 2. Find RECENT files within the monitored folder ---
        # --- 2. Find RECENT files within the monitored folder ---
        # Allow overriding lookback period for testing purposes via a header.
        try:
            lookback_header = request.headers.get('X-Lookback-Minutes')
            lookback_minutes = int(lookback_header) if lookback_header else LOOKBACK_MINUTES
            logging.info(f"Using lookback period of {lookback_minutes} minutes.")
        except (ValueError, TypeError):
            lookback_minutes = LOOKBACK_MINUTES
            logging.warning(f"Invalid X-Lookback-Minutes header. Defaulting to {lookback_minutes} minutes.")

        time_now = datetime.now(timezone.utc)
        time_past = time_now - timedelta(minutes=lookback_minutes)
        time_filter = time_past.isoformat()

        logging.info(f"Searching for new files in folder '{folder_id}' created after {time_filter}")

        response = DRIVE_SERVICE.files().list(
            q=f"'{folder_id}' in parents and createdTime > '{time_filter}' and trashed = false",
            fields='files(id, name, mimeType)',
            orderBy='createdTime'
        ).execute()

        files_to_process = response.get('files', [])
        if not files_to_process:
            logging.info("No new files found to process.")
            return "No new files found", 200

        logging.info(f"Found {len(files_to_process)} new file(s) to process.")

        # --- 3. Loop through and process each new file ---
        for file_item in files_to_process:
            file_id = file_item.get('id')
            file_name = file_item.get('name')
            mime_type = file_item.get('mimeType')
            
            logging.info(f"Processing file: '{file_name}' (ID: {file_id})")

            file_content_stream = io.BytesIO()
            
            if mime_type in EXPORT_MIMETYPES:
                export_config = EXPORT_MIMETYPES[mime_type]
                export_mime_type = export_config['mimeType']
                base_name, _ = os.path.splitext(file_name)
                destination_filename = f"{base_name}{export_config['extension']}"
                logging.info(f"Exporting '{file_name}' to '{destination_filename}'")
                request_handle = DRIVE_SERVICE.files().export_media(
                    fileId=file_id, mimeType=export_mime_type)
            else:
                destination_filename = file_name
                logging.info(f"Downloading binary file: '{file_name}'")
                request_handle = DRIVE_SERVICE.files().get_media(fileId=file_id)

            downloader = MediaIoBaseDownload(file_content_stream, request_handle)
            done = False
            while not done:
                status, done = downloader.next_chunk()
                logging.info(f"Download progress for '{file_name}': {int(status.progress() * 100)}%")

            # --- 4. Upload the File to Google Cloud Storage ---
            logging.info(f"Uploading '{destination_filename}' to GCS bucket '{BUCKET_NAME}'")
            bucket = STORAGE_CLIENT.bucket(BUCKET_NAME)
            blob = bucket.blob(destination_filename)
            file_content_stream.seek(0)
            blob.upload_from_file(file_content_stream)
            logging.info(f"Successfully uploaded {destination_filename} to gs://{BUCKET_NAME}/{destination_filename}")

    except HttpError as error:
        logging.error(f"An HTTP error occurred: {error}")
        return f"Error processing file: {error}", 500
    except Exception as e:
        logging.error(f"An unexpected error occurred: {e}")
        return "Internal Server Error", 500

    return f"Processed {len(files_to_process)} file(s) successfully", 200



# ==============================================================================
# === CSV TO BIGQUERY PROCESSOR (CLOUD EVENT TRIGGER) ==========================
# ==============================================================================

@functions_framework.cloud_event
def drive_process_csv_to_bigquery(cloud_event):
    """
    A generic Cloud Function triggered by a Cloud Storage event.
    It parses a file and loads it into a BigQuery table based on configuration.
    """
    if not STORAGE_CLIENT or not BIGQUERY_CLIENT:
        logging.error("Clients are not initialized. Exiting function.")
        return "Internal Server Error: Clients not initialized", 500

    data = cloud_event.data
    bucket_name = data["bucket"]
    file_name = data["name"]

    logging.info(f"Processing file '{file_name}' from bucket '{bucket_name}'.")

    parser_config = next((p for p in PARSERS.values() if re.match(p['filename_pattern'], file_name)), None)
    
    if not parser_config:
        logging.warning(f"No parser found for file '{file_name}'. Ignoring.")
        return "No parser found", 200

    project_id = parser_config['project_id']
    dataset_id = parser_config['dataset_id']
    table_id = parser_config['table_id']
    table_ref = f"{project_id}.{dataset_id}.{table_id}"

    try:
        bucket = STORAGE_CLIENT.bucket(bucket_name)
        blob = bucket.blob(file_name)
        file_data = blob.download_as_text()

        rows_to_insert = []
        if parser_config['file_type'] == 'csv':
            csv_options = parser_config.get('csv_options', {})
            reader = csv.DictReader(io.StringIO(file_data), delimiter=csv_options.get('delimiter', ','))
            
            for row in reader:
                new_row = {}
                for col_schema in parser_config['schema']:
                    source_col, dest_col, col_type = col_schema['source_column'], col_schema['name'], col_schema['type']
                    raw_value = row.get(source_col)
                    
                    if raw_value is None: continue

                    if col_type == 'DATE':
                        date_format = csv_options.get('date_format', '%Y-%m-%d')
                        new_row[dest_col] = datetime.strptime(raw_value, date_format).strftime('%Y-%m-%d')
                    elif col_type == 'FLOAT':
                        new_row[dest_col] = float(raw_value.replace(',', ''))
                    elif col_type == 'INTEGER':
                        new_row[dest_col] = int(raw_value.replace(',', ''))
                    else:
                        new_row[dest_col] = raw_value.strip()
                rows_to_insert.append(new_row)
        else:
            logging.error(f"Unsupported file type: {parser_config['file_type']}")
            return "Unsupported file type", 400

        if not rows_to_insert:
            logging.info("No rows to insert.")
            return "No data to load", 200

        bq_schema = [bigquery.SchemaField(col['name'], col['type']) for col in parser_config['schema']]

        job_config = bigquery.LoadJobConfig(
            write_disposition=parser_config['write_disposition'],
            schema=bq_schema,
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        )

        logging.info(f"Loading {len(rows_to_insert)} rows into BigQuery table '{table_ref}'")
        
        load_job = BIGQUERY_CLIENT.load_table_from_json(rows_to_insert, table_ref, job_config=job_config)
        load_job.result()

        success_message = f"Successfully loaded {len(rows_to_insert)} rows into {table_ref}"
        logging.info(success_message)
        return success_message, 200

    except Exception as e:
        logging.error(f"An unexpected error occurred: {e}", exc_info=True)
        return "Internal Server Error", 500

