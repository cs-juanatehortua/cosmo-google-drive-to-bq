import io
import functions_framework
import logging
import os
import json
from datetime import datetime, timedelta, timezone
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload
from google.auth import default as get_credentials
from google.cloud import storage
import google.cloud.logging

# --- Configuration ---
with open('config.json', 'r') as f:
    config = json.load(f)

BUCKET_NAME = config['bucket_name']
LOOKBACK_MINUTES = config['lookback_minutes']
FOLDERS_TO_WATCH = config['folders_to_watch']
# ---------------------

# Create a reverse lookup for resource_id to folder_key
RESOURCE_ID_TO_FOLDER_KEY = {
    details['resource_id']: key for key, details in FOLDERS_TO_WATCH.items() if details.get('resource_id')
}

# Initialize clients globally to reuse connections across function invocations
try:
    # Set up Google Cloud Logging
    logging_client = google.cloud.logging.Client()
    logging_client.setup_logging()

    CREDENTIALS, _ = get_credentials(scopes=['https://www.googleapis.com/auth/drive.readonly'])
    DRIVE_SERVICE = build('drive', 'v3', credentials=CREDENTIALS)
    STORAGE_CLIENT = storage.Client()
except Exception as e:
    logging.error(f"Failed to initialize global clients: {e}")
    DRIVE_SERVICE = None
    STORAGE_CLIENT = None

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

@functions_framework.http
def drive_file_downloader(request):
    """
    An HTTP Cloud Function triggered by Google Drive Push Notifications on a FOLDER.
    It finds recent files in that folder, downloads/exports them, and uploads to GCS.
    """
    if not DRIVE_SERVICE or not STORAGE_CLIENT:
        logging.error("Clients are not initialized. Exiting function.")
        return "Internal Server Error: Clients not initialized", 500

    # --- 1. Process Incoming Notification for a FOLDER ---
    resource_state = request.headers.get('X-Goog-Resource-State')
    resource_id = request.headers.get('X-Goog-Resource-ID')
    
    logging.info(f"Received notification: state={resource_state}, resource_id={resource_id}")

    if resource_state != 'update' and resource_state != 'add':
        logging.info(f"Ignoring notification with state: {resource_state}")
        return "Notification ignored", 204

    if not resource_id:
        logging.error("No resource ID (X-Goog-Resource-ID) found in headers.")
        return "Bad Request: Missing resource ID", 400

    # Look up the folder_id using the resource_id
    folder_key = RESOURCE_ID_TO_FOLDER_KEY.get(resource_id)
    if not folder_key:
        logging.error(f"Resource ID '{resource_id}' not found in config.json.")
        return "Bad Request: Unknown resource ID", 400
        
    folder_id = FOLDERS_TO_WATCH[folder_key]['folder_id']
    logging.info(f"Matched resource ID '{resource_id}' to folder '{folder_key}' (ID: {folder_id})")

    try:
        # --- 2. Find RECENT files within the monitored folder ---
        time_now = datetime.now(timezone.utc)
        time_past = time_now - timedelta(minutes=LOOKBACK_MINUTES)
        # Format time for the Drive API query
        time_filter = time_past.isoformat()

        logging.info(f"Searching for new files in folder '{folder_id}' created after {time_filter}")

        # Query for files in the parent folder created within the lookback window
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