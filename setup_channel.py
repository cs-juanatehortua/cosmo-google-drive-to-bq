import uuid
import json
import argparse
from googleapiclient.discovery import build
from google.oauth2 import service_account
from googleapiclient.errors import HttpError

# --- Configuration ---
SCOPES = ['https://www.googleapis.com/auth/drive']
# ---------------------

def setup_watch(folder_id, function_url, key_file_path):
    """Authenticates and creates a push notification channel to watch a Drive folder."""
    
    # Authenticate using the service account credentials
    try:
        creds = service_account.Credentials.from_service_account_file(
            key_file_path, scopes=SCOPES)
        
        # Build the Google Drive API service object
        drive_service = build('drive', 'v3', credentials=creds)

        # Define the request body for the notification channel
        channel_request_body = {
            'id': str(uuid.uuid4()),  # A unique ID for this channel
            'type': 'web_hook',
            'address': function_url,
        }

        print(f"Attempting to watch folder: {folder_id}")
        print(f"Notifications will be sent to: {function_url}")

        # Call the files().watch() method to create the channel
        response = drive_service.files().watch(
            fileId=folder_id,
            body=channel_request_body,
            supportsAllDrives=True # Important if the folder is in a Shared Drive
        ).execute()

        print("\nSuccessfully set up the notification channel.")
        print(f"Channel ID: {response['id']}")
        print(f"Resource ID: {response['resourceId']}")
        
        return response['resourceId']

    except HttpError as error:
        print(f"An error occurred: {error}")
    except FileNotFoundError:
        print(f"Error: The key file was not found at '{key_file_path}'")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
    
    return None

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Set up a Google Drive push notification channel.')
    parser.add_argument('folder_key', type=str, help='The key of the folder to watch from the config file (e.g., monefy_exports)')
    args = parser.parse_args()

    with open('config.json', 'r') as f:
        config = json.load(f)

    folder_to_watch = config['folders_to_watch'][args.folder_key]
    folder_id = folder_to_watch['folder_id']
    function_url = config['function_url']
    key_file_path = config['key_file_path']
    
    resource_id = setup_watch(folder_id, function_url, key_file_path)

    if resource_id:
        folder_to_watch['resource_id'] = resource_id
        with open('config.json', 'w') as f:
            json.dump(config, f, indent=4)
        print(f"\nUpdated config.json with resource_id: {resource_id}")