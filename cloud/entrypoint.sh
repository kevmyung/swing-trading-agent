#!/bin/bash
# cloud/entrypoint.sh — Start AgentCore Runtime via SDK.
#
# Code sync from S3 is handled in cloud/main.py at import time.

set -e

echo "=== Starting AgentCore Runtime ==="
exec python -m cloud.main
