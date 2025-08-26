## Configuration

Create a `config.json` file in the root of the project. This file stores the configuration for the Cloud Function and the folders to be monitored.

### Complete `config.json` Example

```json
{
    "function_url": "https://<YOUR_REGION>-<YOUR_PROJECT_ID>.cloudfunctions.net/<YOUR_FUNCTION_NAME>",
    "key_file_path": "./credentials.json",
    "bucket_name": "<YOUR_GCS_BUCKET_NAME>",
    "lookback_minutes": 5,
    "folders_to_watch": {
        "folder": {
            "folder_id": "<GOOGLE_DRIVE_FOLDER_ID>",
            "resource_id": ""
        }
    }
}
```

-   `function_url`: The trigger URL of your deployed Cloud Function.
-   `key_file_path`: The path to your service account credentials file.
-   `bucket_name`: The name of the Google Cloud Storage bucket where files will be uploaded.
-   `lookback_minutes`: How far back (in minutes) the function should look for new files upon being triggered.
-   `folders_to_watch`: An object containing the folders you want to monitor.
    -   Each key (e.g., `monefy_exports`) is a unique identifier for your folder.
    -   `folder_id`: The actual ID of the Google Drive folder.
    -   `resource_id`: This will be populated automatically by the `setup_channel.py` script. The Cloud Function uses this ID to look up the correct `folder_id`.

## Deployment Instructions

Deploy the Cloud Function using the `gcloud` command-line tool.

    gcloud functions deploy drive_file_downloader \
    --gen2 \
    --runtime python313 \
    --region europe-west1 \
    --source . \
    --entry-point=drive_file_downloader \
    --trigger-http \
    --allow-unauthenticated \
    --service-account=my-service-account@my-project.iam.gserviceaccount.com

## Setting up the Notification Channel

After deploying the function, you must set up a push notification channel for each folder you want to watch. This script tells Google Drive to notify your function when a change occurs in the specified folder.

Run the `setup_channel.py` script with the key of the folder from your `config.json` as an argument.

    python setup_channel.py monefy_exports

The script will then:
1.  Call the Google Drive API to create a notification channel.
2.  Receive a unique `resource_id` for that channel.
3.  Automatically update your `config.json` file, filling in the `resource_id` for the corresponding folder.