#!/usr/bin/env bash
# Remove everything deploy_aws.sh created. Touches nothing else in the account.
#
#   ./scripts/destroy_aws.sh              delete the function, role and URL
#   ./scripts/destroy_aws.sh --bucket     also empty and delete the S3 bucket
#
# The bucket is kept by default because it holds archived contracts. Deleting it
# destroys the artifacts every audit-log row points at.
set -uo pipefail

REGION="${REGION:-ap-south-1}"
FN="${FN:-freelance-guardian}"
ROLE="$FN-lambda-role"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="$FN-$ACCOUNT"

echo "This will delete, in account $ACCOUNT / $REGION:"
echo "  lambda function   $FN (and its Function URL)"
echo "  iam role          $ROLE"
[ "${1:-}" = "--bucket" ] && echo "  s3 bucket         $BUCKET  AND ALL ARCHIVED CONTRACTS"
printf 'Type the function name to confirm: '
read -r reply
[ "$reply" = "$FN" ] || { echo "aborted."; exit 1; }

aws lambda delete-function-url-config --function-name "$FN" --region "$REGION" 2>/dev/null \
  && echo "deleted  function url"
aws lambda delete-function --function-name "$FN" --region "$REGION" 2>/dev/null \
  && echo "deleted  function"

aws iam delete-role-policy --role-name "$ROLE" --policy-name "$FN-s3" 2>/dev/null
aws iam detach-role-policy --role-name "$ROLE" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole 2>/dev/null
aws iam delete-role --role-name "$ROLE" 2>/dev/null && echo "deleted  role"

if [ "${1:-}" = "--bucket" ]; then
  aws s3 rm "s3://$BUCKET" --recursive --region "$REGION" >/dev/null 2>&1
  aws s3api delete-bucket --bucket "$BUCKET" --region "$REGION" 2>/dev/null \
    && echo "deleted  bucket"
else
  echo "kept     s3://$BUCKET  (re-run with --bucket to delete)"
fi

echo "done."
