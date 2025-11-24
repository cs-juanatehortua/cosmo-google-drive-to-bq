import base64
import json
from unittest.mock import MagicMock
import pytest

from main import (
    drive_setup_watch,
    drive_file_downloader,
    FIRESTORE_CLIENT,
    FIRESTORE_COLLECTION,
    _get_secret,
    SECRET_ID,
    PROJECT_ID
)

# pytest -s --log-cli-level=INFO --color=no test.py::test_drive_setup_watch
def test_drive_setup_watch():
    """
    Tests the drive_setup_watch function with a mock CloudEvent.
    """

    # Prepare a dummy Pub/Sub message
    # We use a hardcoded folder_key to make the test independent of config.json content.
    message_data = {
        "folder_key": "monefy",
        "function_url_base": "https://europe-west1-james-gcp-project.cloudfunctions.net/drive_file_downloader"
    }
    message_bytes = json.dumps(message_data).encode('utf-8')
    encoded_data = base64.b64encode(message_bytes)

    # Create a mock CloudEvent object that mimics the structure of a real event.
    # The function expects an object with a 'data' attribute, not a dictionary.
    mock_event = MagicMock()
    mock_event.data = {"message": {"data": encoded_data}}

    # Call the function
    response, status_code = drive_setup_watch(mock_event)

    # Assert a successful response
    assert status_code == 200
    assert response == "Channel setup successful"


# pytest -s --log-cli-level=INFO --color=no test.py::test_drive_file_downloader
def test_drive_file_downloader():
    """
    Tests the drive_file_downloader function with a mock HTTP request.
    This is an INTEGRATION TEST that reads live data from Firestore and Secret Manager.
    """
    # --- 1. Setup: Define which folder to test and get live data ---
    folder_key_to_test = "monefy"  # This must match a key in your config.json and have a watch setup

    # Fetch the live resource_id from Firestore for the given folder
    doc_ref = FIRESTORE_CLIENT.collection(FIRESTORE_COLLECTION).document(folder_key_to_test)
    doc = doc_ref.get()
    assert doc.exists, f"Firestore document for '{folder_key_to_test}' not found. Run drive_setup_watch first."
    resource_id = doc.to_dict().get('resource_id')
    assert resource_id, f"'resource_id' not found in Firestore document for '{folder_key_to_test}'."

    # Fetch the live secret from Secret Manager
    secret = _get_secret(SECRET_ID, PROJECT_ID)
    assert secret, "Failed to retrieve secret from Secret Manager."

    # --- 2. Mock the HTTP Request ---
    mock_request = MagicMock()
    mock_request.headers = {
        'X-Goog-Resource-State': 'update',
        'X-Goog-Resource-ID': resource_id,
        'X-Lookback-Minutes': '120'  # Custom lookback for testing
    }
    mock_request.path = f"/{secret}" # Use the actual secret in the path

    # --- 3. Call the Function ---
    # This test makes real API calls to Google Drive.
    # Ensure your local ADC is authenticated with the correct scopes.
    response, status_code = drive_file_downloader(mock_request)

    # --- 4. Assert the Outcome ---
    # "No new files found" is a success case, meaning the function correctly
    # authenticated, found the folder, and queried Drive.
    assert status_code == 200
    assert "No new files found" in response or "Processed" in response
