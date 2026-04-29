# OTS Pipeline (`ots-pipeline`)

This repository contains the backend data processing components for the Original Tale Studio (OTS) translation service platform. Specifically, it implements the **Fast Track** translation pipeline—a fully automated, NMT-first (Neural Machine Translation) process orchestrated by Cloud Workflows and executed as a sequence of **Google Cloud Run Jobs**.

## Architecture & Components

The pipeline is split into four distinct Cloud Run Jobs, each handling a specific phase of the automated translation process. They share common utility code and dependencies located in the `shared/` directory. 

*   `ft_preprocess`: **Pre-processing** - Segments the source text, extracts metadata, and prepares the document for translation.
*   `ft_nmt`: **Machine Translation** - The core translation engine utilizing Google's Gemini / Vertex AI to perform machine translation (Taiwanese to English, Japanese, or Korean).
*   `ft_qa_auto`: **Automated Quality Assurance** - A 4-layer auto QA process checking structure, semantics, terminology, and utilizing an LLM-as-judge to flag potential issues.
*   `ft_deliver`: **Delivery** - Packages the final translation, generates the necessary outputs, and updates the system state for delivery to the client.
*   `shared/`: Contains common utilities, database connection helpers, and the `requirements.txt` used by all the jobs.

These jobs are written in **Python 3.12** and are containerized using Docker. 

## Prerequisites

*   `gcloud` CLI installed and authenticated.
*   Google Cloud Project (`ots-translation`) with billing enabled.
*   Vertex AI API enabled.
*   Appropriate Infrastructure deployed (via `ots-infra`), including Cloud SQL databases, GCS buckets, and Secret Manager entries.

## Deployment

The pipeline jobs are built using Cloud Build and deployed to Cloud Run Jobs in `asia-east1`. The `deploy_pipeline.sh` script handles the entire build and deployment process.

To deploy the jobs to a specific environment (`dev`, `staging`, or `production`):

```bash
# Make the deployment script executable
chmod +x deploy_pipeline.sh

# Deploy to development
./deploy_pipeline.sh dev

# Deploy to staging
./deploy_pipeline.sh staging

# Deploy to production
./deploy_pipeline.sh production
```

During deployment, the script performs the following actions for each of the 4 jobs:
1. Dynamically generates a Dockerfile based on `Dockerfile.template`.
2. Submits a Cloud Build job to build the Docker image.
3. Creates or updates the Cloud Run Job, injecting necessary environment variables and secrets (`DB_URL`, `GOOGLE_AI_API_KEY`).

## Local Testing / Manual Execution

To trigger a job manually for testing purposes (e.g., executing the preprocess job for a specific order), you can use the `gcloud run jobs execute` command and pass the `ORDER_ID` as an environment variable:

```bash
gcloud run jobs execute ots-ft-preprocess-dev \
    --region=asia-east1 \
    --update-env-vars='ORDER_ID=YOUR_ORDER_ID' \
    --project=ots-translation
```

To view the execution logs for a specific run:

```bash
gcloud run jobs executions logs tail <EXECUTION_NAME> \
    --region=asia-east1 \
    --project=ots-translation
```

## Environment Variables

The deployed Cloud Run Jobs rely on the following environment variables (injected automatically via the deployment script):

| Variable | Description |
| :--- | :--- |
| `PROJECT_ID` | GCP Project ID (e.g., `ots-translation`) |
| `ENV` | Environment name (`dev`, `staging`, `production`) |
| `REGION` | GCP Region (`asia-east1`) |
| `GCS_UPLOADS_BUCKET` | Bucket containing the source files |
| `GCS_OUTPUTS_BUCKET` | Bucket for final translation outputs |
| `GCS_TEMP_BUCKET` | Bucket for temporary files passed between pipeline steps |
| `DB_URL` | Cloud SQL Connection String (injected via Secret Manager) |
| `GOOGLE_AI_API_KEY` | Vertex AI / Gemini API Key (injected via Secret Manager) |
