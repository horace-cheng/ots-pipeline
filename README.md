# OTS Pipeline (`ots-pipeline`)

This repository contains the backend data processing components for the Original Tale Studio (OTS) translation service platform. Specifically, it implements two translation pipelines — **Fast Track** (fully automated NMT pipeline) and **Literary Track** (human-in-the-loop with editor/proofreader) — orchestrated by Cloud Workflows and executed as a sequence of **Google Cloud Run Jobs**.

## Architecture & Components

The pipeline is split into Cloud Run Jobs, each handling a specific phase. They share common utility code and dependencies located in the `shared/` directory.

### Fast Track

| Job | Description |
|-----|-------------|
| `ft_preprocess` | Segment source text, extract metadata, prepare for translation |
| `ft_nmt` | Machine translation via Google Gemini/Vertex AI |
| `ft_qa_auto` | 4-layer auto QA: structure, semantic, terminology, LLM-as-judge |
| `ft_deliver` | Package and deliver: generates TXT, HTML, and bilingual HTML |

### Literary Track

| Job | Description |
|-----|-------------|
| `lt_preprocess_nmt` | Preprocess + NMT (AI draft) — large tier (4Gi/2CPU/3600s) |
| `lt_qa_checklist` | QA checklist evaluation |
| `lt_deliver` | Package and deliver: TXT, HTML, bilingual HTML with l10n labels |

### Shared modules (`shared/`)

| Module | Purpose |
|--------|---------|
| `config.py` | `PipelineConfig` — loads env vars (`ORDER_ID`, `PROJECT_ID`, `WEB_PORTAL_URL`, etc.) |
| `db.py` | DB helpers: `update_job_status()`, `get_order_info()`, `get_db()` |
| `storage.py` | GCS temp/output helpers: `read_temp_json()`, `write_output()`, `aggregate_checkpoints()` |
| `gemini.py` | Gemini/Vertex AI client: `call_gemini()`, `translate()`, `call_gemini_with_file_search()`, `judge()` |
| `requirements.txt` | Shared Python dependencies |

## Prerequisites

*   `gcloud` CLI installed and authenticated.
*   Google Cloud Project (`ots-translation`) with billing enabled.
*   Vertex AI API enabled.
*   Appropriate Infrastructure deployed (via `ots-infra`), including Cloud SQL databases, GCS buckets, and Secret Manager entries.

## Deployment

The pipeline jobs are built using Cloud Build and deployed to Cloud Run Jobs in `asia-east1`. The `deploy_pipeline.sh` script handles the entire build and deployment process.

```bash
# Make the deployment script executable
chmod +x deploy_pipeline.sh

# Deploy all jobs to development
./deploy_pipeline.sh dev

# Deploy only Fast Track jobs
./deploy_pipeline.sh dev ft

# Deploy only Literary Track jobs
./deploy_pipeline.sh dev lt

# Deploy a single job (e.g., ft_nmt)
./deploy_pipeline.sh dev nmt
```

During deployment, the script performs the following actions for each job:
1. Dynamically generates a Dockerfile.
2. Submits a Cloud Build job to build the Docker image.
3. Creates or updates the Cloud Run Job, injecting environment variables and secrets.
4. **Dev only**: Applies GCS lifecycle rules to the temp bucket (7d→Nearline, 30d→Coldline, 60d→Archive, 180d→Delete).

## Local Testing / Manual Execution

To trigger a job manually for testing purposes:

```bash
gcloud run jobs execute ots-ft-preprocess-dev \
    --region=asia-east1 \
    --update-env-vars='ORDER_ID=YOUR_ORDER_ID' \
    --project=ots-translation
```

To view execution logs:

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
| `WEB_PORTAL_URL` | Frontend URL for embedded CSS/font assets in deliverable HTML |
| `DB_URL` | Cloud SQL Connection String (injected via Secret Manager) |
| `GOOGLE_AI_API_KEY` | Vertex AI / Gemini API Key (injected via Secret Manager) |

### Runtime overrides

| Variable | Used by | Purpose |
| :--- | :--- | :--- |
| `ORDER_ID` | All jobs | The order to process |
| `REDELIVER` | `ft_deliver` | `true` to skip QA gate/corpus/notification (admin redeliver) |
