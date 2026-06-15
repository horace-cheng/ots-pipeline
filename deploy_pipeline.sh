#!/usr/bin/env bash
# =============================================================================
# OTS — Pipeline Jobs 部署腳本
# =============================================================================
# 使用方式：
#   ./deploy_pipeline.sh [env]              部署全部 15 個 Jobs (FT 4 + LT 3 + GT 8)
#   ./deploy_pipeline.sh [env] ft           僅部署 Fast Track (4 個)
#   ./deploy_pipeline.sh [env] lt           僅部署 Literary Track (3 個)
#   ./deploy_pipeline.sh [env] gt           僅部署 Gutenberg Track (8 個)
#   ./deploy_pipeline.sh [env] ots-ft-nmt   部署單一 Job (by name/key)
#   ./deploy_pipeline.sh [env] nmt          部署單一 Job (by short key)
#   ./deploy_pipeline.sh [env] --build-only ft  只建 Image 不部署 (加速)
#   ./deploy_pipeline.sh [env] --no-cache   不使用 Docker layer cache (debug)
#   ./deploy_pipeline.sh [env] --machine-type=e2-highcpu-16 gt  自訂機器類型
#
# 軌道總覽：
#   ft_* (4 個 Jobs)   — Fast Track 標準文件翻譯
#   lt_* (3 個 Jobs)   — Literary Track 長文人工校對翻譯
#   gt_* (8 個 Jobs)   — Gutenberg Track 自動翻譯 Project Gutenberg 書籍
#                         gt_fetcher          (EPUB/文字下載，產出 full_text.txt)
#                         gt_chapter_splitter (LLM 偵測章節，產出 chapters.json + segments.json)
#                         gt_extract_terms    (術語提取)
#                         gt_translate        (英→中 標準翻譯，segment-based)
#                         gt_simplify         (中→青少年版，chapter-based)
#                         gt_tailo            (青少年版→Hanzi+台羅拼音，segment-based)
#                         gt_deliver          (產出 7 個交付檔：3 TXT + 4 HTML)
#                         gt_video_prep       (簡化版→影片分鏡腳本，產出 video_materials.json)
#
# 部署速度：
#   - 所有選定的 Job Image 在單一 Cloud Build 中平行建置 (e2-standard-16)
#   - 每個 build 使用 --cache-from=<job>:latest 復用上次的 pip install 層
#   - Deploy 步驟 (gcloud run jobs update) 在 build 完成後平行執行
#   - 重複 deploy 同樣的 code：build 階段 < 30s，total < 2 分鐘
# =============================================================================

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
log()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()   { echo -e "${GREEN}[OK]${NC}    $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()  { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

# ── Wall-clock timing ────────────────────────────────────────────────────────
SCRIPT_START_EPOCH=$(date +%s)
# Print "elapsed: Xm Ys" when the script exits (success or failure).
# Reset cursor to a new line in case the last log line didn't end with \n.
SCRIPT_ELAPSED() {
  local end=$(date +%s)
  local secs=$((end - SCRIPT_START_EPOCH))
  local mins=$((secs / 60))
  local rem=$((secs % 60))
  if [[ $mins -gt 0 ]]; then
    echo -e "${GREEN}[TIME]${NC}  Total elapsed: ${mins}m ${rem}s (${secs}s)"
  else
    echo -e "${GREEN}[TIME]${NC}  Total elapsed: ${rem}s"
  fi
}
trap 'SCRIPT_ELAPSED' EXIT

# ── Parse args ───────────────────────────────────────────────────────────────
ENV=""
# FILTER is now a list — accepts multiple job keys/names
FILTERS=()
BUILD_ONLY=false
USE_CACHE=true
# Default to e2-highcpu-8 — fits all 14 parallel docker builds within
# Cloud Build's default quota. Use --machine-type=e2-highcpu-16 if you
# have quota and want faster builds (e2-highcpu-32 also works).
MACHINE_TYPE="e2-highcpu-8"
for arg in "$@"; do
  case "$arg" in
    dev|staging|production) ENV="$arg" ;;
    --build-only)           BUILD_ONLY=true ;;
    --no-cache)             USE_CACHE=false ;;
    --machine-type=*)       MACHINE_TYPE="${arg#--machine-type=}" ;;
    ft|lt|gt)               FILTERS+=("$arg") ;;
    --help|-h)
      sed -n '2,30p' "$0" | sed 's/^# \?//'
      exit 0
      ;;
    *) FILTERS+=("$arg") ;;
  esac
done

[[ "$ENV" =~ ^(dev|staging|production)$ ]] || \
  err "請指定環境：./deploy_pipeline.sh [dev|staging|production]"

PROJECT_ID="ots-translation"
REGION="asia-east1"
REGISTRY="asia-east1-docker.pkg.dev/${PROJECT_ID}/ots"
SA_PIPELINE="ots-pipeline-${ENV}@${PROJECT_ID}.iam.gserviceaccount.com"
SA_API="ots-api-backend-${ENV}@${PROJECT_ID}.iam.gserviceaccount.com"
SQL_INSTANCE="${PROJECT_ID}:${REGION}:ots-db-${ENV}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# DOCKER_DIR is relative to SCRIPT_DIR (the gcloud build context root).
# Absolute paths fail inside Cloud Build's clean build environment.
DOCKER_DIR=".docker"

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
  # Gutenberg Track (Gutenberg book translation)
  "gt_fetcher:ots-gt-fetcher-${ENV}:gt_fetcher:standard"
  "gt_chapter_splitter:ots-gt-chapter-splitter-${ENV}:gt_chapter_splitter:large"
  "gt_extract_terms:ots-gt-extract-terms-${ENV}:gt_extract_terms:standard"
  "gt_translate:ots-gt-translate-${ENV}:gt_translate:standard"
  "gt_simplify:ots-gt-simplify-${ENV}:gt_simplify:large"
  "gt_tailo:ots-gt-tailo-${ENV}:gt_tailo:large"
  "gt_deliver:ots-gt-deliver-${ENV}:gt_deliver:standard"
  "gt_video_prep:ots-gt-video-prep-${ENV}:gt_video_prep:large"
)

# ── Filter to selected jobs ─────────────────────────────────────────────────
# Match logic: a job is selected if ANY filter matches it.
#   ft/lt/gt       → all jobs in that track
#   <key>          → exact match on job_key
#   <substring>    → substring match on job_name
SELECTED=()
for job_spec in "${JOBS[@]}"; do
  IFS=':' read -r job_key job_name job_dir job_tier <<< "$job_spec"

  if [[ ${#FILTERS[@]} -gt 0 ]]; then
    matched=false
    for f in "${FILTERS[@]}"; do
      case "$f" in
        ft) [[ "$job_key" != lt_* && "$job_key" != gt_* ]] && matched=true ;;
        lt) [[ "$job_key" == lt_* ]] && matched=true ;;
        gt) [[ "$job_key" == gt_* ]] && matched=true ;;
        *)  [[ "$job_key" == "$f" || "$job_name" == *"$f"* ]] && matched=true ;;
      esac
    done
    [[ "$matched" == "false" ]] && continue
  fi
  SELECTED+=("$job_spec")
done

[[ ${#SELECTED[@]} -eq 0 ]] && err "沒有符合篩選條件的 Job — 名稱可能錯誤"

echo ""
echo -e "${CYAN}=====================================================${NC}"
echo -e "${CYAN}  OTS Pipeline Deploy — ENV: ${YELLOW}${ENV}${NC}"
echo -e "${CYAN}  Jobs: ${YELLOW}${#SELECTED[@]}${CYAN} | Cache: ${YELLOW}$( [[ $USE_CACHE == true ]] && echo on || echo off )${CYAN} | Build-only: ${YELLOW}$BUILD_ONLY${CYAN}"
if [[ ${#FILTERS[@]} -gt 0 ]]; then
  echo -e "${CYAN}  Filters: ${YELLOW}${FILTERS[*]}${NC}"
fi
echo -e "${CYAN}  Machine type: ${YELLOW}${MACHINE_TYPE}${NC}"
echo -e "${CYAN}=====================================================${NC}"
echo ""

# ── 授予 Pipeline SA 必要的 Vertex AI 權限 ───────────────────────────────────
# Permissions are global — only grant if deploying at least one job
log "啟用 Vertex AI API 並確認 Pipeline SA 權限..."
gcloud services enable aiplatform.googleapis.com --project="$PROJECT_ID" --quiet
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_PIPELINE}" \
  --role="roles/aiplatform.user" \
  --condition=None \
  --quiet
ok "Vertex AI 權限確認完成"

# ── 設定 Pipeline Temp Bucket 的生命週期規則（僅 dev）────────────────────────
if [[ "$ENV" == "dev" ]]; then
  TEMP_BUCKET="${PROJECT_ID}-pipeline-temp-${ENV}"
  log "設定 Temp Bucket 生命週期規則: ${TEMP_BUCKET}..."
  LIFECYCLE_FILE="$(mktemp)"
  cat > "$LIFECYCLE_FILE" <<'LIFECYCLE'
{
  "lifecycle": {
    "rule": [
      {"action": {"type": "SetStorageClass", "storageClass": "NEARLINE"}, "condition": {"age": 7}},
      {"action": {"type": "SetStorageClass", "storageClass": "COLDLINE"},  "condition": {"age": 30}},
      {"action": {"type": "SetStorageClass", "storageClass": "ARCHIVE"},   "condition": {"age": 60}},
      {"action": {"type": "Delete"},                                       "condition": {"age": 180}}
    ]
  }
}
LIFECYCLE
  if gsutil ls "gs://${TEMP_BUCKET}" &>/dev/null; then
    gsutil lifecycle set "$LIFECYCLE_FILE" "gs://${TEMP_BUCKET}"
    ok "Temp bucket lifecycle rules applied: 7d→Nearline 30d→Coldline 60d→Archive 180d→Delete"
  else
    warn "Temp bucket ${TEMP_BUCKET} does not exist — skipping lifecycle setup"
  fi
  rm -f "$LIFECYCLE_FILE"
else
  log "Skipping temp bucket lifecycle rules for ${ENV} (dev only)"
fi

# ── 為每個選定的 Job 產生 Dockerfile ──────────────────────────────────────────
mkdir -p "$DOCKER_DIR"
for job_spec in "${SELECTED[@]}"; do
  IFS=':' read -r job_key job_name job_dir job_tier <<< "$job_spec"

  # gt_chapter_splitter 額外依賴 gt_fetcher（reuses 它的 _strip_gutenberg_boilerplate
  # 和 split_text_structured 作為 LLM 失敗時的 regex fallback）。
  EXTRA_COPY=""
  if [[ "$job_key" == "gt_chapter_splitter" ]]; then
    EXTRA_COPY="COPY gt_fetcher/ ./gt_fetcher/"
  fi
  cat > "${DOCKER_DIR}/Dockerfile.${job_key}" <<DOCKERFILE
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONPATH="/app/ots-common:\$PYTHONPATH"
WORKDIR /app
COPY shared/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY shared/ ./shared/
COPY ots-common/ ./ots-common/
COPY ${job_dir}/ ./job/
${EXTRA_COPY}
CMD ["python", "job/main.py"]
DOCKERFILE
done
ok "已產生 ${#SELECTED[@]} 個 Dockerfile 在 ${DOCKER_DIR}/"

# ── 產生平行 cloudbuild.yaml ─────────────────────────────────────────────────
CLOUDBUILD_FILE="${SCRIPT_DIR}/cloudbuild.gen.yaml"
COMMON_ENV="PROJECT_ID=${PROJECT_ID},ENV=${ENV},REGION=${REGION}"
COMMON_ENV+=",GCS_UPLOADS_BUCKET=${PROJECT_ID}-uploads-${ENV}"
COMMON_ENV+=",GCS_OUTPUTS_BUCKET=${PROJECT_ID}-outputs-${ENV}"
COMMON_ENV+=",GCS_TEMP_BUCKET=${PROJECT_ID}-pipeline-temp-${ENV}"
COMMON_ENV+=",WEB_PORTAL_URL=https://ots-frontend-${ENV}-miptn5nxpa-de.a.run.app"

# Discover the actual API URL from Cloud Run (hash varies per project).
# The API must be deployed before the pipeline.
API_URL=$(gcloud run services describe "ots-api-backend-${ENV}" \
  --region="$REGION" --project="$PROJECT_ID" \
  --format='value(status.url)' 2>/dev/null) || {
  warn "Could not discover API URL from Cloud Run. Falling back to hardcoded pattern."
  API_URL="https://ots-api-backend-${ENV}-${PROJECT_ID}.asia-east1.run.app"
}
COMMON_ENV+=",API_BASE_URL=${API_URL}"

# Pass 1: write the header + images: block
# Note: machineType is passed via --machine-type on the gcloud CLI flag
# (Cloud Build config validator rejects the options.machineType field).
cat > "$CLOUDBUILD_FILE" <<HEADER
# Auto-generated by deploy_pipeline.sh — do not edit.
# All selected job images build in parallel; deploys run in parallel after
# each build completes.

substitutions:
  _COMMON_ENV: '${COMMON_ENV}'
  _SQL_INSTANCE: '${SQL_INSTANCE}'
  _SA_PIPELINE: '${SA_PIPELINE}'
  _REGION: '${REGION}'
  _PROJECT: '${PROJECT_ID}'

images: [
HEADER
for job_spec in "${SELECTED[@]}"; do
  IFS=':' read -r job_key job_name _ <<< "$job_spec"
  printf "  '%s',\n" "${REGISTRY}/${job_name}:latest" >> "$CLOUDBUILD_FILE"
done
printf "]\n\n" >> "$CLOUDBUILD_FILE"

# Pass 2: write build steps
printf "steps:\n" >> "$CLOUDBUILD_FILE"
for job_spec in "${SELECTED[@]}"; do
  IFS=':' read -r job_key job_name _ <<< "$job_spec"
  IMAGE="${REGISTRY}/${job_name}:latest"

  if [[ "$USE_CACHE" == "true" ]]; then
    CACHE_ARGS="      - '--cache-from'
      - '${IMAGE}'
"
  else
    CACHE_ARGS=""
  fi

  cat >> "$CLOUDBUILD_FILE" <<BUILD_STEP
  - id: 'build-${job_key}'
    name: 'gcr.io/cloud-builders/docker'
    args:
      - 'build'
${CACHE_ARGS}      - '-t'
      - '${IMAGE}'
      - '-f'
      - '${DOCKER_DIR}/Dockerfile.${job_key}'
      - '.'
    waitFor: ['-']

BUILD_STEP
done

# Pass 3: write deploy steps (skipped in build-only mode)
if [[ "$BUILD_ONLY" == "false" ]]; then
  for job_spec in "${SELECTED[@]}"; do
    IFS=':' read -r job_key job_name job_dir job_tier <<< "$job_spec"
    IMAGE="${REGISTRY}/${job_name}:latest"

    if [[ "$job_tier" == "large" ]]; then
      MEMORY="4Gi"; CPU="2"; TIMEOUT=7200
    else
      MEMORY="1Gi"; CPU="1"; TIMEOUT=1800
    fi

    cat >> "$CLOUDBUILD_FILE" <<DEPLOY_STEP
  - id: 'deploy-${job_key}'
    name: 'gcr.io/google.com/cloudsdktool/cloud-sdk:slim'
    entrypoint: 'gcloud'
    args:
      - 'run'
      - 'jobs'
      - 'deploy'
      - '${job_name}'
      - '--image=${IMAGE}'
      - '--region=\${_REGION}'
      - '--project=\${_PROJECT}'
      - '--service-account=\${_SA_PIPELINE}'
      - '--set-cloudsql-instances=\${_SQL_INSTANCE}'
      - '--network=default'
      - '--subnet=default'
      - '--vpc-egress=private-ranges-only'
      - '--set-secrets=DB_URL=ots-db-url-${ENV}:latest,GOOGLE_AI_API_KEY=ots-google-ai-key-${ENV}:latest,BRONCI_API_USERNAME=ots-bronci-username-${ENV}:latest,BRONCI_API_PASSWORD=ots-bronci-password-${ENV}:latest,BRONCI_API_BASE_URL=ots-bronci-base-url-${ENV}:latest,HF_API_TOKEN=ots-hf-api-token-${ENV}:latest,REPLICATE_API_TOKEN=ots-replicate-api-token-${ENV}:latest,NVIDIA_API_TOKEN=ots-nvidia-api-token-${ENV}:latest'
      - '--set-env-vars=\${_COMMON_ENV}'
      - '--max-retries=5'
      - '--task-timeout=${TIMEOUT}'
      - '--memory=${MEMORY}'
      - '--cpu=${CPU}'
      - '--quiet'
    waitFor: ['build-${job_key}']

DEPLOY_STEP
  done

  # Grant API SA developer role on deliver + all Gutenberg jobs so the
  # admin rerun-stage endpoint can trigger any pipeline stage directly.
  # waitFor must be a YAML list of step IDs — not a comma-separated string.
  #
  # NOTE: This step requires the Cloud Build service account to have
  # `roles/run.admin` (or `run.jobs.setIamPolicy`) on the project.
  # The default Compute Engine SA does NOT have this permission.
  # To make this fully automatic, grant the Cloud Build SA that role
  # (see change log 2026-06-07_redeliver_gutenberg.md).  We intentionally
  # do NOT `set -e` here — a single IAM failure must not break the build.
  cat >> "$CLOUDBUILD_FILE" <<IAM_HEADER
  - id: 'grant-deliver-iam'
    name: 'gcr.io/google.com/cloudsdktool/cloud-sdk:slim'
    entrypoint: 'bash'
    args:
      - '-c'
      - |
        set +e
        echo '>>> Granting roles/run.developer to ${SA_API} on deliver + Gutenberg jobs...'
IAM_HEADER
  for job_spec in "${SELECTED[@]}"; do
    IFS=':' read -r job_key job_name _ <<< "$job_spec"
    if [[ "$job_key" == "deliver" || "$job_key" == "lt_deliver" || "$job_key" == gt_* ]]; then
      cat >> "$CLOUDBUILD_FILE" <<IAM_LINE
        if gcloud run jobs add-iam-policy-binding '${job_name}' \\
            --region=\${_REGION} --project=\${_PROJECT} \\
            --member='serviceAccount:${SA_API}' \\
            --role='roles/run.developer' --quiet 2>&1; then
          echo "  [ok]  ${job_name}"
        else
          echo "  [WARN] Failed to grant role on ${job_name}."
          echo "         The Cloud Build SA lacks 'run.jobs.setIamPolicy'."
          echo "         Re-run manually with: gcloud run jobs add-iam-policy-binding ..."
        fi
IAM_LINE
    fi
  done
  cat >> "$CLOUDBUILD_FILE" <<IAM_FOOTER
        echo '>>> IAM grant step complete (failures are non-blocking).'
IAM_FOOTER
  # Build the waitFor list as a proper YAML array
  cat >> "$CLOUDBUILD_FILE" <<IAM_WAITFOR
    waitFor: [
IAM_WAITFOR
  for job_spec in "${SELECTED[@]}"; do
    IFS=':' read -r job_key _ <<< "$job_spec"
    cat >> "$CLOUDBUILD_FILE" <<IAM_DEP
      'deploy-${job_key}',
IAM_DEP
  done
  # Replace trailing comma with closing bracket on a new line
  # (sed -i in-place won't work safely on a long heredoc; use python)
  python3 -c "
import sys
content = open('${CLOUDBUILD_FILE}').read()
# Find the last ',\n' before the waitFor close and replace the last comma
# Actually simpler: find the last ',\n      ]' pattern
content = content.replace(',\n    ]\n', '\n    ]\n', 1)
open('${CLOUDBUILD_FILE}', 'w').write(content)
"
  printf "    ]\n\n" >> "$CLOUDBUILD_FILE"

else
  log "Build-only mode — skipping deploy steps"
fi

ok "已產生平行 cloudbuild.yaml: ${CLOUDBUILD_FILE}"

# ── 提交 Cloud Build ─────────────────────────────────────────────────────────
log "提交 Cloud Build（${#SELECTED[@]} 個 Image 平行建置）..."
BUILD_START=$(date +%s)
gcloud builds submit "${SCRIPT_DIR}" \
  --config="$CLOUDBUILD_FILE" \
  --project="$PROJECT_ID" \
  --machine-type="$MACHINE_TYPE" \
  --gcs-log-dir="gs://${PROJECT_ID}-pipeline-temp-${ENV}/build-logs" \
  --quiet
BUILD_END=$(date +%s)
BUILD_SECS=$((BUILD_END - BUILD_START))
log "Cloud Build 完成: ${BUILD_SECS}s"

rm -f "$CLOUDBUILD_FILE"
# Keep .docker/ for next run (cache) — only clean on --no-cache
if [[ "$USE_CACHE" == "false" ]]; then
  rm -rf "$DOCKER_DIR"
fi
ok "Build 完成 — ${#SELECTED[@]} 個 Image 已更新"

# ── 輸出摘要 ──────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}=====================================================${NC}"
echo -e "${GREEN}  Pipeline 部署完成 — ENV: ${YELLOW}${ENV}${NC}"
echo -e "${GREEN}  Jobs: ${YELLOW}${#SELECTED[@]}${GREEN} | Build-only: ${YELLOW}$BUILD_ONLY${NC}"
if [[ ${#FILTERS[@]} -gt 0 ]]; then
  echo -e "${GREEN}  Filters: ${YELLOW}${FILTERS[*]}${NC}"
fi
echo -e "${GREEN}=====================================================${NC}"
echo ""
echo "  部署的 Jobs（${#SELECTED[@]} 個）："
for job_spec in "${SELECTED[@]}"; do
  IFS=':' read -r job_key job_name _ <<< "$job_spec"
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
echo "  gcloud builds log <BUILD_ID> --project=${PROJECT_ID}"
echo "  gcloud run jobs executions logs tail <EXECUTION_NAME> \\"
echo "    --region=${REGION} --project=${PROJECT_ID}"
echo ""
