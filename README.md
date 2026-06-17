## Architecture Overview

Blog Entry: <a>https://www.jibbyjames.com/post/google-drive-to-bigquery/</a>

This project creates a fully automated, configuration-driven ETL pipeline to transfer files from a Google Drive folder into a BigQuery table. The architecture is event-driven and built on three serverless Cloud Functions:

1.  **`drive_setup_watch_pubsub`**: This function is triggered by a message on a Pub/Sub topic. Its job is to establish a secure communication channel between the Google Drive API and our application. It tells the Drive API to send a notification to a unique, secret-protected URL whenever a change occurs in a specified folder. The details of this channel are stored in Firestore for persistence.

2.  **`drive_file_downloader`**: This is a secure HTTP-triggered function that listens for push notifications from the Google Drive API. When a notification arrives, it first validates a secret in the URL to prevent unauthorized access. It then finds any new files in the monitored Drive folder, downloads them, and uploads them to a Google Cloud Storage (GCS) bucket.

3.  **`drive_process_csv_to_bigquery`**: This function is triggered by the creation of a new file in the GCS bucket. It reads the file, matches it against a set of configured parsers in `config.json`, transforms the data according to the defined schema, and loads the final result into the appropriate BigQuery table.

## Configuration

The entire pipeline is controlled by a single `config.json` file.

-   **`project_id`**: The ID of your Google Cloud Project. This is used to explicitly initialize the cloud clients.
-   **`secret_id`**: The name of the secret stored in Google Cloud Secret Manager. This secret is used to secure the webhook URL for the `drive_file_downloader` function.
-   **`lookback_minutes`**: When a notification is received from Google Drive, the function will search for any files created in the folder within this many minutes in the past. This helps ensure no files are missed.
-   **`bucket_name`**: The name of the Google Cloud Storage bucket where files downloaded from Google Drive will be stored.
-   **`firestore_collection`**: The name of the Firestore collection used to store the state of the Google Drive watch channels (e.g., `channel_id`, `resource_id`).
-   **`folders_to_watch`**: An object where each key represents a logical name for a folder you want to monitor.
    -   **`folder_id`**: The actual ID of the Google Drive folder.
    -   **`resource_id`**: This is populated automatically by the `setup_drive_watch_pubsub` function when a watch channel is created. You should leave it as an empty string initially.
-   **`parsers`**: This section defines the rules for parsing different types of files once they land in the GCS bucket. Each key in this object should correspond to a key in `folders_to_watch`.
    -   **`filename_pattern`**: A regular expression used to match against the name of the file. This determines which parser logic to apply.
    -   **`file_type`**: The type of the file (e.g., `csv`).
    -   **`project_id`**, **`dataset_id`**, **`table_id`**: The full BigQuery path where the parsed data will be loaded.
    -   **`write_disposition`**: Specifies the action that occurs if the destination table already exists. Common values are `WRITE_TRUNCATE` (overwrite) and `WRITE_APPEND`.
    -   **`csv_options`**: A nested object for CSV-specific settings.
        -   `delimiter`: The delimiter used in the CSV file (e.g., `,`).
        -   `date_format`: The format of date strings in the source CSV (e.g., `%d/%m/%Y`).
    -   **`schema`**: An array defining the mapping from the source file columns to the BigQuery table columns.
        -   `name`: The name of the destination column in BigQuery.
        -   `type`: The data type of the destination column (e.g., `DATE`, `STRING`, `FLOAT`).
        -   `source_column`: The name of the corresponding column in the source CSV file.

## Deployment Instructions

### Prerequisites

Before deploying the functions, ensure you have the following resources set up in your GCP project.

1.  **Enable Required APIs**: Ensure the necessary Google Cloud APIs are enabled for your project.

    ```bash
    gcloud services enable \
        drive.googleapis.com \
        secretmanager.googleapis.com \
        pubsub.googleapis.com \
        storage.googleapis.com \
        firestore.googleapis.com \
        bigquery.googleapis.com \
        cloudfunctions.googleapis.com \
        cloudbuild.googleapis.com \
        eventarc.googleapis.com \
        logging.googleapis.com \
        iam.googleapis.com
    ```

2.  **Create a Secret in Secret Manager**: This secret is used to secure the webhook URL for the `drive_file_downloader` function.

    ```bash
    # Create a secure random value for the secret
    SECRET_VALUE=$(openssl rand -hex 32)

    # Create the secret in GCP (replace 'drive-webhook-secret' if you changed it in config.json)
    gcloud secrets create drive-webhook-secret --replication-policy="automatic"

    # Add the first version of the secret
    echo -n "$SECRET_VALUE" | gcloud secrets versions add drive-webhook-secret --data-file=-
    ```

3.  **Create a Pub/Sub Topic**: This topic is used to trigger the setup of a new Google Drive watch channel.

    ```bash
    gcloud pubsub topics create drive-setup-watch
    ```

4.  **Create a GCS Bucket**: This bucket will store the files downloaded from Google Drive before they are processed into BigQuery.

    ```bash
    # Note: Bucket names must be globally unique.
    gcloud storage buckets create gs://jb-g-drive-to-bq --project=james-gcp-project --location=eu
    ```

5.  **Grant Permissions**: The service account used by the functions (`google-drive@james-gcp-project.iam.gserviceaccount.com`) needs the following roles to perform its tasks. Additionally, any user deploying these functions will need the `Service Account User` role.
    *   `roles/secretmanager.secretAccessor`: Allows the function to read the webhook secret from Secret Manager.
    *   `roles/storage.objectAdmin`: Allows the function to write downloaded files to the GCS bucket.
    *   `roles/bigquery.dataEditor`: Allows the function to insert data into BigQuery tables.
    *   `roles/datastore.user`: Allows the function to write watch channel details to Firestore.
    *   `roles/eventarc.eventReceiver`: Allows the function to receive events from event providers like Cloud Storage.
    *   `roles/cloudfunctions.invoker`: Allows the service account to invoke HTTP-triggered functions.
    *   `roles/iam.serviceAccountUser`: Required for any user or service that needs to deploy or act as this service account.

6.  **Grant Drive Access to the Service Account**: For the functions to be able to read from your Google Drive folder, you must share that folder with the service account.
    *   Go to Google Drive and find the folder you want to monitor.
    *   Click the "Share" button.
    *   In the "Add people and groups" field, paste the email address of your service account (e.g., `google-drive@james-gcp-project.iam.gserviceaccount.com`).
    *   Ensure the service account is given at least "Viewer" permissions.
    *   Click "Send".

### 1. Deploy the `drive_file_downloader` Function

This function receives notifications from Google Drive. Note the `--set-env-vars` flag, which provides the base URL that the `setup_drive_watch_pubsub` function will use to construct the full, secure webhook URL.

    gcloud functions deploy drive_file_downloader \
        --gen2 \
        --runtime python313 \
        --region europe-west1 \
        --source . \
        --entry-point=drive_file_downloader \
        --trigger-http \
        --allow-unauthenticated \
        --service-account=google-drive@james-gcp-project.iam.gserviceaccount.com

### 2. Deploy the `drive_setup_watch` Function

This function is triggered by a Pub/Sub message to create or refresh a watch channel on a Google Drive folder.

    gcloud functions deploy drive_setup_watch \
        --gen2 \
        --runtime python313 \
        --region europe-west1 \
        --source . \
        --entry-point=drive_setup_watch \
        --trigger-topic=drive-setup-watch \
        --allow-unauthenticated \
        --ingress-settings=internal-only \
        --service-account=google-drive@james-gcp-project.iam.gserviceaccount.com \
        --set-env-vars FUNCTION_URL_BASE=https://europe-west1-james-gcp-project.cloudfunctions.net/drive_file_downloader

### 3. Deploy the `drive_process_csv_to_bigquery` Function

This function is triggered by file uploads to the GCS bucket and processes the data into BigQuery.

    gcloud functions deploy drive_process_csv_to_bigquery \
        --gen2 \
        --runtime python313 \
        --region europe-west1 \
        --source . \
        --entry-point=drive_process_csv_to_bigquery \
        --trigger-event-filters="type=google.cloud.storage.object.v1.finalized" \
        --trigger-event-filters="bucket=jb-g-drive-to-bq" \
        --trigger-location eu \
        --allow-unauthenticated \
        --ingress-settings=internal-only \
        --service-account=google-drive@james-gcp-project.iam.gserviceaccount.com

## Setting up the Notification Channel

To start watching a folder defined in your `config.json`, publish a message to the `setup-drive-watch` Pub/Sub topic. The message body must contain the `folder_key` you want to set up.

    gcloud pubsub topics publish setup-drive-watch --message '{"folder_key": "monefy"}'

## Local Testing

When running the functions locally, your Application Default Credentials (ADC) are used. By default, these credentials may not have the required permissions for Google Drive, even if your user account does.

### 1. Authenticate Your Local Environment

To grant the necessary scopes to your local ADC, run the following command and follow the authentication flow in your browser. This is crucial for avoiding `Insufficient Permission` errors.

```bash
gcloud auth application-default login --scopes=https://www.googleapis.com/auth/drive,https://www.googleapis.com/auth/cloud-platform
```

### 2. Run Tests

The project includes a `test.py` file to run the function locally. They simulate the Pub/Sub trigger event and can be run using `pytest`.
