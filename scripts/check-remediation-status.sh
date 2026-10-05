#!/bin/bash
# Check PDF-to-PDF remediation status in the existing PDFAccessibility stack.
# Run in AWS CloudShell, region us-east-1. Does not start or stop any services.
#
#   chmod +x scripts/check-remediation-status.sh
#   ./scripts/check-remediation-status.sh

set -euo pipefail

echo "=== Bucket ==="
BUCKET=$(aws s3api list-buckets --query "Buckets[?contains(Name, 'pdfaccessibility')].Name" --output text | awk '{print $NF}')
echo "Bucket: $BUCKET"
echo

echo "=== pdf/ (uploads) ==="
aws s3 ls "s3://${BUCKET}/pdf/" --recursive || true
echo

echo "=== result/ (done) ==="
aws s3 ls "s3://${BUCKET}/result/" --recursive || true
echo

echo "=== temp/ (in progress, last 30) ==="
aws s3 ls "s3://${BUCKET}/temp/" --recursive | tail -30 || true
echo

echo "=== Step Functions (last 5) ==="
SM=$(aws stepfunctions list-state-machines --query "stateMachines[?contains(name, 'PdfAccessibility')].stateMachineArn" --output text)
echo "State machine: $SM"
aws stepfunctions list-executions --state-machine-arn "$SM" --max-results 5 \
  --query "executions[].{status:status,start:startDate,stop:stopDate,name:name}" --output table
echo

echo "=== Latest execution error (if failed) ==="
EXEC=$(aws stepfunctions list-executions --state-machine-arn "$SM" --max-results 1 --query "executions[0].executionArn" --output text)
echo "Execution: $EXEC"
aws stepfunctions describe-execution --execution-arn "$EXEC" --query "{status:status,error:error,cause:cause}" --output json
echo

echo "=== Autotag logs (last 1h) ==="
echo "Look for: local extract (PyMuPDF)  and  STATS"
echo "Should NOT see: Running Adobe Extract API"
aws logs tail /ecs/pdf-remediation/adobe-autotag --since 1h --format short || true
echo

echo "=== Stats JSON in S3 ==="
aws s3 ls "s3://${BUCKET}/temp/" --recursive | grep remediation_stats || echo "(none yet — post-check has not finished)"
echo

echo "Done. RUNNING = still processing. SUCCEEDED + result/COMPLIANT_*.pdf = remediating finished."
