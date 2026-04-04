#!/usr/bin/env bash
# deploy.sh — One-command CDK deployment for Swing Trading Agent
#
# Usage:
#   ./deploy.sh              # deploy all stacks (full Docker rebuild)
#   ./deploy.sh storage      # deploy StorageStack only
#   ./deploy.sh runtime      # deploy RuntimeStack only
#   ./deploy.sh sync-code    # sync code to S3 (fast, no rebuild)
#
# Prerequisites:
#   - AWS CLI configured (aws sts get-caller-identity)
#   - Node.js 18+
#   - npm

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$SCRIPT_DIR/infra"
CONFIG_DIR="$SCRIPT_DIR/config"
CLOUD_CONFIG="$CONFIG_DIR/cloud_resources.json"

PROJECT_NAME="swing-trading-agent"
ENVIRONMENT="dev"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[deploy]${NC} $*"; }
warn() { echo -e "${YELLOW}[deploy]${NC} $*"; }
err()  { echo -e "${RED}[deploy]${NC} $*" >&2; }

# ── Preflight checks ────────────────────────────────────────────

check_prereqs() {
    local missing=0

    if ! command -v aws &>/dev/null; then
        err "aws CLI not found. Install: https://aws.amazon.com/cli/"
        missing=1
    fi
    if ! command -v node &>/dev/null; then
        err "node not found. Install Node.js 18+."
        missing=1
    fi
    if ! command -v npm &>/dev/null; then
        err "npm not found."
        missing=1
    fi

    if [ $missing -ne 0 ]; then
        exit 1
    fi

    # Verify AWS credentials
    if ! aws sts get-caller-identity &>/dev/null; then
        err "AWS credentials not configured. Run: aws configure"
        exit 1
    fi

    ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
    REGION=$(aws configure get region 2>/dev/null || echo "us-west-2")
    log "Account: $ACCOUNT_ID | Region: $REGION"
}

# ── Install CDK dependencies ────────────────────────────────────

install_deps() {
    log "Installing CDK dependencies..."
    cd "$INFRA_DIR"
    if [ ! -d node_modules ]; then
        npm install
    else
        npm install --prefer-offline --no-audit 2>/dev/null || npm install
    fi
    cd "$SCRIPT_DIR"
}

# ── Bootstrap CDK (if needed) ───────────────────────────────────

bootstrap_cdk() {
    log "Checking CDK bootstrap..."
    cd "$INFRA_DIR"
    if ! npx cdk bootstrap "aws://$ACCOUNT_ID/$REGION" 2>&1 | grep -q "already bootstrapped"; then
        log "CDK bootstrap complete."
    fi
    cd "$SCRIPT_DIR"
}

# ── Recover stuck CloudFormation stacks ───────────────────────

_recover_stack_if_needed() {
    local stack_name="$1"
    local status
    status=$(aws cloudformation describe-stacks \
        --stack-name "$stack_name" \
        --query "Stacks[0].StackStatus" \
        --output text 2>/dev/null || echo "NOT_FOUND")

    if [ "$status" = "UPDATE_ROLLBACK_FAILED" ]; then
        warn "Stack '$stack_name' is in UPDATE_ROLLBACK_FAILED state."
        warn "Running continue-update-rollback to recover..."
        if ! aws cloudformation continue-update-rollback --stack-name "$stack_name" 2>/dev/null; then
            warn "Simple rollback failed. Detecting stuck resources..."
            local stuck_resources
            stuck_resources=$(aws cloudformation describe-stack-events \
                --stack-name "$stack_name" \
                --query "StackEvents[?ResourceStatus=='UPDATE_FAILED'].LogicalResourceId" \
                --output text 2>/dev/null | tr '\t' ' ' | tr -s ' ')
            if [ -n "$stuck_resources" ]; then
                warn "Skipping stuck resources: $stuck_resources"
                # shellcheck disable=SC2086
                aws cloudformation continue-update-rollback \
                    --stack-name "$stack_name" \
                    --resources-to-skip $stuck_resources
            fi
        fi
        log "Waiting for rollback to complete..."
        aws cloudformation wait stack-rollback-complete --stack-name "$stack_name"
        log "Stack recovered to UPDATE_ROLLBACK_COMPLETE. Proceeding with deploy."
    elif [ "$status" = "ROLLBACK_FAILED" ]; then
        warn "Stack '$stack_name' is in ROLLBACK_FAILED state (initial create failed)."
        warn "Deleting stack so CDK can recreate it..."
        aws cloudformation delete-stack --stack-name "$stack_name"
        aws cloudformation wait stack-delete-complete --stack-name "$stack_name"
        log "Stack deleted. CDK will create it fresh."
    fi
}

# ── Deploy stacks ───────────────────────────────────────────────

deploy_storage() {
    log "Deploying StorageStack (S3 + DynamoDB)..."
    cd "$INFRA_DIR"
    _recover_stack_if_needed "${PROJECT_NAME}-storage"
    npx cdk deploy "${PROJECT_NAME}-storage" --require-approval never --outputs-file "$INFRA_DIR/outputs-storage.json"
    log "StorageStack deployed."
}

deploy_runtime() {
    log "Deploying RuntimeStack (ECR + CodeBuild + AgentCore Runtime)..."
    cd "$INFRA_DIR"
    _recover_stack_if_needed "${PROJECT_NAME}-runtime"
    npx cdk deploy "${PROJECT_NAME}-runtime" --require-approval never --outputs-file "$INFRA_DIR/outputs-runtime.json"
    log "RuntimeStack deployed."
}

# ── Write cloud_resources.json from stack outputs ───────────────

write_cloud_config() {
    log "Writing cloud config..."

    local storage_outputs="$INFRA_DIR/outputs-storage.json"
    local runtime_outputs="$INFRA_DIR/outputs-runtime.json"

    local bucket_name=""
    local session_table=""
    local runtime_arn=""

    if [ -f "$storage_outputs" ]; then
        bucket_name=$(python3 -c "
import json
d = json.load(open('$storage_outputs'))
stack = d.get('${PROJECT_NAME}-storage', {})
print(stack.get('DataBucketName', ''))
" 2>/dev/null || true)
        session_table=$(python3 -c "
import json
d = json.load(open('$storage_outputs'))
stack = d.get('${PROJECT_NAME}-storage', {})
print(stack.get('SessionTableName', ''))
" 2>/dev/null || true)
    fi

    if [ -f "$runtime_outputs" ]; then
        runtime_arn=$(python3 -c "
import json
d = json.load(open('$runtime_outputs'))
stack = d.get('${PROJECT_NAME}-runtime', {})
print(stack.get('RuntimeArn', ''))
" 2>/dev/null || true)
    fi

    # Fallback: read from SSM if output files don't have values
    if [ -z "$bucket_name" ]; then
        bucket_name=$(aws ssm get-parameter \
            --name "/${PROJECT_NAME}/${ENVIRONMENT}/s3/data-bucket-name" \
            --query Parameter.Value --output text 2>/dev/null || echo "")
    fi
    if [ -z "$session_table" ]; then
        session_table=$(aws ssm get-parameter \
            --name "/${PROJECT_NAME}/${ENVIRONMENT}/dynamodb/session-table-name" \
            --query Parameter.Value --output text 2>/dev/null || echo "")
    fi
    if [ -z "$runtime_arn" ]; then
        runtime_arn=$(aws ssm get-parameter \
            --name "/${PROJECT_NAME}/${ENVIRONMENT}/agentcore/runtime-arn" \
            --query Parameter.Value --output text 2>/dev/null || echo "")
    fi

    mkdir -p "$CONFIG_DIR"
    cat > "$CLOUD_CONFIG" <<EOF
{
  "s3_bucket": "$bucket_name",
  "session_table": "$session_table",
  "agentcore_runtime_arn": "$runtime_arn",
  "region": "$REGION"
}
EOF

    log "Cloud config written to: $CLOUD_CONFIG"
    cat "$CLOUD_CONFIG"
}

# ── Sync code to S3 (fast deploy — no Docker rebuild) ─────────

sync_code() {
    log "Syncing code to S3..."

    if [ ! -f "$CLOUD_CONFIG" ]; then
        err "Cloud config not found: $CLOUD_CONFIG"
        err "Run './deploy.sh' first to set up cloud resources."
        exit 1
    fi

    local bucket
    bucket=$(python3 -c "import json; print(json.load(open('$CLOUD_CONFIG')).get('s3_bucket',''))")
    if [ -z "$bucket" ]; then
        err "s3_bucket not found in cloud config"
        exit 1
    fi

    local prefix="code/swing-trading-agent"
    local s3_base="s3://${bucket}/${prefix}"
    local sync_opts="--exclude __pycache__/* --exclude *.pyc --delete --quiet"

    # Sync only runtime-relevant directories
    for dir in agents cloud config providers scheduler state store tools playbook; do
        if [ -d "$SCRIPT_DIR/$dir" ]; then
            aws s3 sync "$SCRIPT_DIR/$dir/" "$s3_base/$dir/" $sync_opts
        fi
    done

    # backtest/ — only .py files (not fixtures/sessions)
    aws s3 sync "$SCRIPT_DIR/backtest/" "$s3_base/backtest/" \
        --exclude "*" --include "*.py" --delete --quiet

    # Root files
    aws s3 cp "$SCRIPT_DIR/main.py" "$s3_base/main.py" --quiet
    aws s3 cp "$SCRIPT_DIR/requirements.txt" "$s3_base/requirements.txt" --quiet

    log "Code synced to $s3_base/"
    log "Next container start will pick up the new code."
}

# ── Main ────────────────────────────────────────────────────────

main() {
    local target="${1:-all}"

    # Fast path: sync-code doesn't need CDK
    if [ "$target" = "sync-code" ]; then
        check_prereqs
        sync_code
        exit 0
    fi

    echo ""
    log "=========================================="
    log "  Swing Trading Agent — CDK Deployment"
    log "=========================================="
    echo ""

    check_prereqs
    install_deps
    bootstrap_cdk

    case "$target" in
        storage)
            deploy_storage
            write_cloud_config
            ;;
        runtime)
            deploy_runtime
            write_cloud_config
            sync_code
            ;;
        all)
            deploy_storage
            deploy_runtime
            write_cloud_config
            sync_code
            ;;
        *)
            err "Unknown target: $target"
            echo "Usage: ./deploy.sh [all|storage|runtime|sync-code]"
            exit 1
            ;;
    esac

    echo ""
    log "=========================================="
    log "  Deployment complete!"
    log "=========================================="
    log "Cloud config: $CLOUD_CONFIG"
    echo ""
}

main "$@"
