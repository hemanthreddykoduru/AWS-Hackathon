#!/usr/bin/env bash
# Deploy Freelance Guardian to AWS Lambda + S3. Idempotent — safe to re-run.
#
#   ./scripts/deploy_aws.sh          build, deploy, print the Function URL
#   ./scripts/deploy_aws.sh --code   skip the pip build, just re-upload src/
#
# Everything it creates is free-tier eligible and uniquely named, so it cannot
# collide with other stacks in the account. To remove it all: ./scripts/destroy_aws.sh
set -euo pipefail

REGION="${REGION:-ap-south-1}"          # same region as the CockroachDB cluster
FN="${FN:-freelance-guardian}"
ROLE="$FN-lambda-role"
ARCH="arm64"                             # Graviton: cheaper, and all our wheels exist for it
RUNTIME="python3.11"

cd "$(dirname "$0")/.."
[ -f .env ] || { echo "no .env — copy .env.example and set COCKROACH_DB_URL"; exit 1; }
# shellcheck disable=SC1091
DB_URL="$(grep -E '^COCKROACH_DB_URL=' .env | cut -d= -f2-)"
[ -n "$DB_URL" ] || { echo "COCKROACH_DB_URL is empty in .env"; exit 1; }

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="$FN-$ACCOUNT"
echo "account $ACCOUNT · region $REGION · bucket $BUCKET"

# ---------------------------------------------------------------- 1. S3 bucket
if aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  echo "bucket   exists"
else
  aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
    --create-bucket-configuration LocationConstraint="$REGION" >/dev/null
  # Contracts are private. Block every form of public access explicitly.
  aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  echo "bucket   created"
fi

# ------------------------------------------------------------------- 2. IAM role
if aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  echo "role     exists"
else
  aws iam create-role --role-name "$ROLE" --assume-role-policy-document '{
    "Version":"2012-10-17",
    "Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]
  }' >/dev/null
  aws iam attach-role-policy --role-name "$ROLE" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
  echo "role     created"
fi

# Least privilege: this bucket only, and only the two actions src/s3.py performs.
aws iam put-role-policy --role-name "$ROLE" --policy-name "$FN-s3" --policy-document "{
  \"Version\":\"2012-10-17\",
  \"Statement\":[{
    \"Effect\":\"Allow\",
    \"Action\":[\"s3:PutObject\",\"s3:GetObject\"],
    \"Resource\":\"arn:aws:s3:::$BUCKET/*\"
  }]
}"
ROLE_ARN="$(aws iam get-role --role-name "$ROLE" --query Role.Arn --output text)"

# ---------------------------------------------------------------- 3. build zip
if [ "${1:-}" = "--code" ] && [ -d build/pkg ]; then
  echo "build    reusing build/pkg (--code)"
else
  echo "build    installing linux/$ARCH wheels …"
  rm -rf build && mkdir -p build/pkg
  PY="$([ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)"
  "$PY" -m pip install --platform manylinux2014_aarch64 --only-binary=:all: \
    --python-version 3.11 --target build/pkg -q -r requirements.txt
  # boto3/botocore/s3transfer ship with the Lambda runtime — 28MB we do not upload.
  # NOTE: do NOT delete *.dist-info. SQLAlchemy resolves the "cockroachdb" dialect
  # through entry points declared in sqlalchemy_cockroachdb's dist-info; removing it
  # gets you NoSuchModuleError at runtime and nowhere near the cause.
  ( cd build/pkg && rm -rf boto3 botocore s3transfer boto3-* botocore-* s3transfer-* \
      && find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null
      find . -name '*.pyc' -delete 2>/dev/null
      find . -name 'tests' -type d -prune -exec rm -rf {} + 2>/dev/null ) || true
fi

rm -rf build/pkg/src build/pkg/sample_data build/function.zip
cp -r src build/pkg/
# /api/sample serves the sample contract from inside the package.
cp -r sample_data build/pkg/

# Ship the CockroachDB CA inside the package. The Lambda sandbox has neither
# ~/.postgresql/root.crt nor this CA in its system trust store, so verify-full fails
# both ways without it. /var/task is where Lambda unpacks the deployment package.
CERT="${CERT:-$HOME/.postgresql/root.crt}"
[ -f "$CERT" ] || { echo "no CA cert at $CERT — download it from the CockroachDB Cloud
  console (Connect > Download CA Cert), or set CERT=/path/to/root.crt"; exit 1; }
cp "$CERT" build/pkg/root.crt
find build/pkg/src -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
( cd build/pkg && zip -qr ../function.zip . -x '*.DS_Store' )
echo "build    $(du -h build/function.zip | cut -f1) zipped"

# Direct upload caps at 50MB; go via S3 when the package outgrows it.
ZIP_BYTES=$(wc -c < build/function.zip)
if [ "$ZIP_BYTES" -gt 49000000 ]; then
  aws s3 cp build/function.zip "s3://$BUCKET/deploy/function.zip" --region "$REGION" >/dev/null
  CODE_ARGS=(--s3-bucket "$BUCKET" --s3-key deploy/function.zip)
else
  CODE_ARGS=(--zip-file fileb://build/function.zip)
fi

# ------------------------------------------------------------------ 4. function
# sslmode=verify-full looks for ~/.postgresql/root.crt, which does not exist in the
# Lambda sandbox. CockroachDB Cloud is signed by a public CA, so the container's system
# trust store validates it — sslrootcert=system keeps full verification without shipping
# a cert. Local runs keep using the downloaded root.crt; only the deployed copy differs.
case "$DB_URL" in
  *sslrootcert=*) LAMBDA_DB_URL="$DB_URL" ;;
  *\?*)           LAMBDA_DB_URL="$DB_URL&sslrootcert=/var/task/root.crt" ;;
  *)              LAMBDA_DB_URL="$DB_URL?sslrootcert=/var/task/root.crt" ;;
esac

# AWS_REGION is set by the Lambda runtime and cannot be overridden here.
ENV_VARS="Variables={MOCK_MODE=true,S3_BUCKET=$BUCKET,COCKROACH_DB_URL=$LAMBDA_DB_URL}"

if aws lambda get-function --function-name "$FN" --region "$REGION" >/dev/null 2>&1; then
  echo "function updating code …"
  aws lambda update-function-code --function-name "$FN" --region "$REGION" \
    "${CODE_ARGS[@]}" --architectures "$ARCH" >/dev/null
  aws lambda wait function-updated --function-name "$FN" --region "$REGION"
  aws lambda update-function-configuration --function-name "$FN" --region "$REGION" \
    --environment "$ENV_VARS" --timeout 120 --memory-size 1536 >/dev/null
else
  echo "function creating (retrying while the IAM role propagates) …"
  for attempt in 1 2 3 4 5 6; do
    if aws lambda create-function --function-name "$FN" --region "$REGION" \
        --runtime "$RUNTIME" --architectures "$ARCH" --role "$ROLE_ARN" \
        --handler src.lambda_function.handler "${CODE_ARGS[@]}" \
        --environment "$ENV_VARS" --timeout 120 --memory-size 1536 >/dev/null 2>&1; then
      break
    fi
    [ "$attempt" = 6 ] && { echo "create-function failed after 6 attempts"; exit 1; }
    sleep 10
  done
fi
aws lambda wait function-updated --function-name "$FN" --region "$REGION"

# --------------------------------------------------------------- 5. Function URL
if ! aws lambda get-function-url-config --function-name "$FN" --region "$REGION" >/dev/null 2>&1; then
  aws lambda create-function-url-config --function-name "$FN" --region "$REGION" \
    --auth-type NONE \
    `# AllowMethods entries are capped at 6 characters, so "OPTIONS" is rejected — "*" it is.` \
    --cors 'AllowOrigins=["*"],AllowMethods=["*"],AllowHeaders=["content-type"],MaxAge=3600' >/dev/null
  # A NONE-auth URL still needs an explicit resource policy allowing public invoke.
  aws lambda add-permission --function-name "$FN" --region "$REGION" \
    --statement-id FunctionURLAllowPublicAccess --action lambda:InvokeFunctionUrl \
    --principal '*' --function-url-auth-type NONE >/dev/null 2>&1 || true
fi

URL="$(aws lambda get-function-url-config --function-name "$FN" --region "$REGION" \
  --query FunctionUrl --output text)"

echo
echo "deployed: $URL"
echo "test:     curl -sX POST '$URL' -H 'content-type: application/json' \\"
echo "            -d '{\"contract_text\":\"Unlimited revisions. Net 60.\",\"client_name\":\"Test Co\"}'"
