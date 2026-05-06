#!/usr/bin/env bash
# =============================================================================
# OTS — Fast Track Pipeline Jobs 部署腳本
# =============================================================================
# 建置並部署 4 個 Cloud Run Jobs：
#   ots-ft-preprocess, ots-ft-nmt, ots-ft-qa-auto, ots-ft-deliver
#
# 使用方式：./deploy_pipeline.sh [dev|staging|production]
# =============================================================================

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
log()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()   { echo -e "${GREEN}[OK]${NC}    $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()  { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

ENV="${1:-}"
[[ "$ENV" =~ ^(dev|staging|production)$ ]] || \
  err "請指定環境：./deploy_pipeline.sh [dev|staging|production]"

PROJECT_ID="ots-translation"
REGION="asia-east1"
REGISTRY="asia-east1-docker.pkg.dev/${PROJECT_ID}/ots"
SA_PIPELINE="ots-pipeline-${ENV}@${PROJECT_ID}.iam.gserviceaccount.com"
SQL_INSTANCE="${PROJECT_ID}:${REGION}:ots-db-${ENV}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Cloud Run Jobs 設定
# Format: job_key:job_name:job_dir:tier  (tier=standard|large)
JOBS=(
  # Fast Track (standard resources)
  "preprocess:ots-ft-preprocess-${ENV}:ft_preprocess:standard"
  "nmt:ots-ft-nmt-${ENV}:ft_nmt:standard"
  "qa_auto:ots-ft-qa-auto-${ENV}:ft_qa_auto:standard"
  "deliver:ots-ft-deliver-${ENV}:ft_deliver:standard"
  # Literary Track (large resources for 10K+ word files)
  "lt_preprocess_nmt:ots-lt-preprocess-nmt-${ENV}:lt_preprocess_nmt:large"
  "lt_qa_checklist:ots-lt-qa-checklist-${ENV}:lt_qa_checklist:standard"
  "lt_deliver:ots-lt-deliver-${ENV}:lt_deliver:standard"
)

echo ""
echo -e "${CYAN}=====================================================${NC}"
echo -e "${CYAN}  OTS Pipeline Deploy — ENV: ${YELLOW}${ENV}${NC}"
echo -e "${CYAN}=====================================================${NC}"
echo ""

# ── 授予 Pipeline SA 必要的 Vertex AI 權限 ───────────────────────────────────
log "啟用 Vertex AI API 並確認 Pipeline SA 權限..."
gcloud services enable aiplatform.googleapis.com --project="$PROJECT_ID" --quiet
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_PIPELINE}" \
  --role="roles/aiplatform.user" \
  --condition=None \
  --quiet
ok "Vertex AI 權限確認完成"

# ── 建置並部署每個 Job ────────────────────────────────────────────────────────
for job_spec in "${JOBS[@]}"; do
  IFS=':' read -r job_key job_name job_dir job_tier <<< "$job_spec"

  log "Building image for ${job_name}..."
  IMAGE="${REGISTRY}/${job_name}:latest"

  # 建立臨時 Dockerfile
  cat > "${SCRIPT_DIR}/Dockerfile.tmp" << DOCKERFILE
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY shared/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY shared/ ./shared/
COPY ${job_dir}/ ./job/
CMD ["python", "job/main.py"]
DOCKERFILE

  # 建立臨時 cloudbuild config
  IMAGE_ESCAPED="$IMAGE"
  cat > "${SCRIPT_DIR}/cloudbuild.tmp.yaml" <<CLOUDBUILD
steps:
  - name: 'gcr.io/cloud-builders/docker'
    args: ['build', '-t', '${IMAGE_ESCAPED}', '-f', 'Dockerfile.tmp', '.']
images:
  - '${IMAGE_ESCAPED}'
CLOUDBUILD

  gcloud builds submit "${SCRIPT_DIR}" \
    --config="${SCRIPT_DIR}/cloudbuild.tmp.yaml" \
    --project="$PROJECT_ID" \
    --gcs-log-dir="gs://${PROJECT_ID}-pipeline-temp-${ENV}/build-logs" \
    --quiet

  rm -f "${SCRIPT_DIR}/Dockerfile.tmp" "${SCRIPT_DIR}/cloudbuild.tmp.yaml"
  ok "Image built: ${IMAGE}"

  # 共用環境變數
  COMMON_ENV="PROJECT_ID=${PROJECT_ID},ENV=${ENV},REGION=${REGION}"
  COMMON_ENV+=",GCS_UPLOADS_BUCKET=${PROJECT_ID}-uploads-${ENV}"
  COMMON_ENV+=",GCS_OUTPUTS_BUCKET=${PROJECT_ID}-outputs-${ENV}"
  COMMON_ENV+=",GCS_TEMP_BUCKET=${PROJECT_ID}-pipeline-temp-${ENV}"

  # 根據 tier 設定資源
  if [[ "$job_tier" == "large" ]]; then
    MEMORY="4Gi"
    CPU="2"
    TIMEOUT=3600
  else
    MEMORY="1Gi"
    CPU="1"
    TIMEOUT=1800
  fi

  # 建立或更新 Cloud Run Job
  if gcloud run jobs describe "$job_name" \
       --region="$REGION" --project="$PROJECT_ID" --quiet &>/dev/null; then
    log "Updating existing Job: ${job_name} (tier=${job_tier}, mem=${MEMORY}, cpu=${CPU}, timeout=${TIMEOUT}s)..."
    gcloud run jobs update "$job_name" \
      --image="$IMAGE" \
      --region="$REGION" \
      --project="$PROJECT_ID" \
      --service-account="$SA_PIPELINE" \
      --set-cloudsql-instances="$SQL_INSTANCE" \
      --network=default \
      --subnet=default \
      --vpc-egress=private-ranges-only \
      --set-secrets="DB_URL=ots-db-url-${ENV}:latest,GOOGLE_AI_API_KEY=ots-google-ai-key:latest" \
      --set-env-vars="$COMMON_ENV" \
      --max-retries=2 \
      --task-timeout="$TIMEOUT" \
      --memory="$MEMORY" \
      --cpu="$CPU" \
      --quiet
  else
    log "Creating new Job: ${job_name} (tier=${job_tier}, mem=${MEMORY}, cpu=${CPU}, timeout=${TIMEOUT}s)..."
    gcloud run jobs create "$job_name" \
      --image="$IMAGE" \
      --region="$REGION" \
      --project="$PROJECT_ID" \
      --service-account="$SA_PIPELINE" \
      --set-cloudsql-instances="$SQL_INSTANCE" \
      --network=default \
      --subnet=default \
      --vpc-egress=private-ranges-only \
      --set-secrets="DB_URL=ots-db-url-${ENV}:latest,GOOGLE_AI_API_KEY=ots-google-ai-key:latest" \
      --set-env-vars="$COMMON_ENV" \
      --max-retries=2 \
      --task-timeout="$TIMEOUT" \
      --memory="$MEMORY" \
      --cpu="$CPU" \
      --quiet
  fi

  ok "Job deployed: ${job_name}"
  echo ""
done

# ── 輸出摘要 ──────────────────────────────────────────────────────────────────
echo -e "${GREEN}=====================================================${NC}"
echo -e "${GREEN}  Pipeline 部署完成 — ENV: ${YELLOW}${ENV}${NC}"
echo -e "${GREEN}=====================================================${NC}"
echo ""
echo "  部署的 Jobs："
for job_spec in "${JOBS[@]}"; do
  IFS=':' read -r _ job_name _ <<< "$job_spec"
  echo "    ${job_name}"
done
echo ""
echo -e "${YELLOW}  單案手動測試：${NC}"
echo "  gcloud run jobs execute ots-ft-preprocess-${ENV} \\"
echo "    --region=${REGION} \\"
echo "    --update-env-vars='ORDER_ID=YOUR_ORDER_ID' \\"
echo "    --project=${PROJECT_ID}"
echo ""
echo -e "${YELLOW}  查看執行 log：${NC}"
echo "  gcloud run jobs executions logs tail <EXECUTION_NAME> \\"
echo "    --region=${REGION} --project=${PROJECT_ID}"
echo ""
