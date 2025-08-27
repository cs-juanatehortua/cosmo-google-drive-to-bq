## Architecture Overview

This project creates a fully automated, configuration-driven ETL pipeline to transfer files from a Google Drive folder into a BigQuery table. It consists of two separate Cloud Functions:

1.  **`drive_file_downloader`**: This function is triggered by a push notification from the Google Drive API when a file is added to a specified folder. It downloads the file and uploads it to a Google Cloud Storage (GCS) bucket.
2.  **`process_csv_to_bigquery`**: This function is triggered when a new file is created in the GCS bucket. It reads the file, matches it against a set of configured parsers, and loads the transformed data into the corresponding BigQuery table.

## Configuration

The entire pipeline is controlled by a single `config.json` file.

### Complete `config.json` Example

```json
{
    "function_url": "https://<YOUR_REGION>-<YOUR_PROJECT_ID>.cloudfunctions.net/drive_file_downloader",
    "key_file_path": "./credentials.json",
    "bucket_name": "<YOUR_GCS_BUCKET_NAME>",
    "lookback_minutes": 5,
    "folders_to_watch": {
        "monefy": {
            "folder_id": "<GOOGLE_DRIVE_FOLDER_ID>",
            "resource_id": ""
        }
    },
    "parsers": {
        "monefy": {
            "filename_pattern": "^monefy_export_.*\\.csv$",
            "file_type": "csv",
            "project_id": "james-gcp-project",
            "dataset_id": "monefy",
            "table_id": "transactions",
            "write_disposition": "WRITE_TRUNCATE",
            "csv_options": {
                "delimiter": ",",
                "date_format": "%d/%m/%Y"
            },
            "schema": [
                {"name": "date", "type": "DATE", "source_column": "date"},
                {"name": "account", "type": "STRING", "source_column": "account"},
                {"name": "category", "type": "STRING", "source_column": "category"},
                {"name": "amount", "type": "FLOAT", "source_column": "amount"},
                {"name": "description", "type": "STRING", "source_column": "description"}
            ]
        }
    }
}
```

-   **`parsers`**: This section defines the rules for parsing different types of files.
    -   **`filename_pattern`**: A regular expression used to match against the name of the file uploaded to GCS. This determines which parser logic to apply.
    -   **`file_type`**: The type of the file (e.g., `csv`). This allows for extending the logic to other types like `json` in the future.
    -   **`csv_options`**: A nested object for CSV-specific settings.
        -   `delimiter`: The delimiter used in the CSV file.
        -   `date_format`: The format of date strings in the source CSV.
    -   **`schema`**: An array defining the mapping from the source file to the BigQuery table.

## Deployment Instructions

### 1. Deploy the `drive_file_downloader` Function

    gcloud functions deploy drive_file_downloader \
    --gen2 \
    --runtime python313 \
    --region europe-west1 \
    --source . \
    --entry-point=drive_file_downloader \
    --trigger-http \
    --allow-unauthenticated \
    --service-account=my-service-account@my-project.iam.gserviceaccount.com

### 2. Deploy the `process_csv_to_bigquery` Function

    gcloud functions deploy process_csv_to_bigquery \
    --gen2 \
    --runtime python313 \
    --region europe-west1 \
    --source . \
    --entry-point=process_csv_to_bigquery \
    --trigger-event-filters="type=google.cloud.storage.object.v1.finalized" \
    --trigger-event-filters="bucket=<YOUR_GCS_BUCKET_NAME>" \
    --trigger-location eu \
    --service-account=my-service-account@my-project.iam.gserviceaccount.com \
    --allow-unauthenticated

## Setting up the Notification Channel

After deploying the `drive_file_downloader` function, run the `setup_channel.py` script for each folder you want to watch.

    python setup_channel.py monefy