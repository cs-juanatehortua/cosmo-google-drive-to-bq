import main
from unittest.mock import MagicMock

BUCKET_NAME = main.BUCKET_NAME

# pytest test_gcs_to_gbq.py::test_csv_to_bigquery -s --log-cli-level=INFO --disable-warnings
def test_csv_to_bigquery():
    """
    Integration test for the process_csv_to_bigquery function.
    It uses a real GCS file and writes to a real BigQuery table.
    It assumes the test file already exists in the GCS bucket.
    """
    # Create a mock object that has a 'data' attribute
    mock_cloud_event = MagicMock()
    mock_cloud_event.data = {
        "bucket": BUCKET_NAME,
        "name": "Monefy.Data.28-09-2024.csv"
    }

    # --- 2. Call the function ---
    result, status_code = main.process_csv_to_bigquery(mock_cloud_event)

    # --- 3. Assertions ---
    assert status_code == 200
    assert "Successfully loaded" in result