#!/usr/bin/env bash
# Prints the DNS record to add when hypersynaptic.com is NOT hosted in Route 53.
set -euo pipefail
DOMAIN="${DOMAIN:-mediascrape.hypersynaptic.com}"
TARGET=$(aws --region us-east-1 cloudformation describe-stacks --stack-name mediascrape-app \
  --query "Stacks[0].Outputs[?OutputKey=='DistributionDomain'].OutputValue" --output text)
cat <<MSG
Add this record at your DNS provider:

  Type:  CNAME
  Name:  ${DOMAIN%%.*}        (i.e. $DOMAIN)
  Value: $TARGET
MSG
