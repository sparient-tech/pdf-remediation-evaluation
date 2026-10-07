#!/bin/bash
# After deploy + upload: did veraPDF skip Adobe Autotag?
# Run in AWS CloudShell, us-east-1. Does not start or stop services.
#
#   chmod +x scripts/check-adobe-skip.sh
#   ./scripts/check-adobe-skip.sh

set -euo pipefail

BUCKET=$(aws s3api list-buckets --query "Buckets[?contains(Name, 'pdfaccessibility')].Name" --output text | awk '{print $NF}')
echo "Bucket: $BUCKET"
echo

echo "=== Latest veraPDF summaries ==="
mapfile -t KEYS < <(aws s3api list-objects-v2 --bucket "$BUCKET" --prefix temp/ \
  --query "reverse(sort_by(Contents[?contains(Key, 'verapdf_summary.json')], &LastModified))[:5].Key" \
  --output text | tr '\t' '\n')

if [ -z "${KEYS[0]:-}" ] || [ "${KEYS[0]}" = "None" ]; then
  echo "No veraPDF summary yet. Wait until the Autotag ECS task finishes, then rerun."
  echo
else
  for KEY in "${KEYS[@]}"; do
    [ -z "$KEY" ] && continue
    echo "--- s3://${BUCKET}/${KEY} ---"
    aws s3 cp "s3://${BUCKET}/${KEY}" - | python3 -c '
import json,sys
d=json.load(sys.stdin)
print("file:", d.get("source_pdf"))
print("compliant:", d.get("is_compliant"))
print("has_structure_tree:", d.get("has_structure_tree"))
print("failed_rules:", d.get("failed_rules"))
print("tagging_failures:", len(d.get("tagging_failed_rules") or []))
print("other_failures:", len(d.get("other_failed_rules") or []))
print("call_adobe_autotag:", d.get("call_adobe_autotag"))
print("decision_reason:", d.get("decision_reason"))
'
    echo
  done
fi

echo "=== Autotag logs: skip vs call (last 2h) ==="
echo "SKIP = 'Skipping Adobe Autotag'"
echo "CALL = 'Running Adobe Autotag API'"
aws logs tail /ecs/pdf-remediation/adobe-autotag --since 2h --format short \
  | grep -E "veraPDF decision|Skipping Adobe Autotag|Running Adobe Autotag API|STATS " || true
echo

echo "=== How to read ==="
echo "Skip worked: call_adobe_autotag=false  AND  log line Skipping Adobe Autotag"
echo "Adobe ran:   call_adobe_autotag=true   AND  log line Running Adobe Autotag API"
echo "Adobe console Autotag transactions should stay flat when skip worked."
