#!/usr/bin/env bash
# One-time AWS setup for mediascrape. Run from the repo root on your laptop:
#
#   ./scripts/bootstrap.sh
#
# It is safe to re-run. It:
#   1. creates the GitHub -> AWS trust (OIDC) and a deploy role, plus the ECR
#      repository for the Lambda image (CloudFormation stack "mediascrape-bootstrap")
#   2. requests the HTTPS certificate for the domain in us-east-1 and validates
#      it through Route 53 when the zone is hosted there (otherwise prints the
#      DNS record for you to add)
#   3. prints (and, if the GitHub CLI is logged in, sets) the repo variables and
#      secrets the deploy workflow reads.
#
# Needs: AWS CLI v2 with admin-ish credentials for the account (aws configure / SSO).
# Optional: gh (GitHub CLI) to set repo variables for you.

set -euo pipefail

REGION=us-east-1                       # CloudFront certificates must live here
DOMAIN="${DOMAIN:-mediascrape.hypersynaptic.com}"
PARENT_DOMAIN="${PARENT_DOMAIN:-${DOMAIN#*.}}"
REPO="${REPO:-marksuguitan/mediascrape}"
BOOTSTRAP_STACK=mediascrape-bootstrap
HERE="$(cd "$(dirname "$0")/.." && pwd)"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
aws_() { aws --region "$REGION" "$@"; }

command -v aws >/dev/null || { echo "The AWS CLI is not installed." >&2; exit 1; }

say "Checking AWS credentials"
ACCOUNT=$(aws_ sts get-caller-identity --query Account --output text)
echo "Account $ACCOUNT, region $REGION, domain $DOMAIN"

# --------------------------------------------------------------- 1. bootstrap
say "Creating the deploy role and image repository (stack $BOOTSTRAP_STACK)"
OIDC_ARN="arn:aws:iam::${ACCOUNT}:oidc-provider/token.actions.githubusercontent.com"
CREATE_OIDC=true
if aws iam get-open-id-connect-provider --open-id-connect-provider-arn "$OIDC_ARN" >/dev/null 2>&1; then
  # Already present. Keep creating it only if this very stack owns it.
  OWNER=$(aws_ cloudformation describe-stack-resources --stack-name "$BOOTSTRAP_STACK" \
            --logical-resource-id GitHubOidcProvider --query 'StackResources[0].PhysicalResourceId' \
            --output text 2>/dev/null || true)
  [ "$OWNER" = "$OIDC_ARN" ] || CREATE_OIDC=false
fi

aws_ cloudformation deploy \
  --stack-name "$BOOTSTRAP_STACK" \
  --template-file "$HERE/infra/bootstrap.yml" \
  --capabilities CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset \
  --parameter-overrides "GitHubRepo=$REPO" "CreateOidcProvider=$CREATE_OIDC"

ROLE_ARN=$(aws_ cloudformation describe-stacks --stack-name "$BOOTSTRAP_STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='DeployRoleArn'].OutputValue" --output text)
echo "Deploy role: $ROLE_ARN"

# --------------------------------------------------------------- 2. certificate
say "Looking for a Route 53 hosted zone for $PARENT_DOMAIN"
ZONE_ID=$(aws route53 list-hosted-zones-by-name --dns-name "$PARENT_DOMAIN." \
  --query "HostedZones[?Name=='${PARENT_DOMAIN}.' && Config.PrivateZone==\`false\`].Id | [0]" \
  --output text 2>/dev/null || true)
if [ -z "$ZONE_ID" ] || [ "$ZONE_ID" = "None" ]; then
  ZONE_ID=""
  echo "Not in Route 53: you will add two DNS records at your DNS provider by hand."
else
  ZONE_ID="${ZONE_ID##*/}"
  echo "Found zone $ZONE_ID: DNS records will be created automatically."
fi

say "Getting an HTTPS certificate for $DOMAIN"
CERT_ARN=$(aws_ acm list-certificates \
  --certificate-statuses ISSUED PENDING_VALIDATION \
  --query "CertificateSummaryList[?DomainName=='${DOMAIN}'].CertificateArn | [0]" --output text)
if [ -z "$CERT_ARN" ] || [ "$CERT_ARN" = "None" ]; then
  CERT_ARN=$(aws_ acm request-certificate --domain-name "$DOMAIN" \
    --validation-method DNS --query CertificateArn --output text)
  echo "Requested $CERT_ARN"
  sleep 10   # ACM needs a moment before it shows the validation record
fi
echo "Certificate: $CERT_ARN"

STATUS=$(aws_ acm describe-certificate --certificate-arn "$CERT_ARN" \
  --query Certificate.Status --output text)
if [ "$STATUS" != "ISSUED" ]; then
  read -r VNAME VVALUE < <(aws_ acm describe-certificate --certificate-arn "$CERT_ARN" \
    --query 'Certificate.DomainValidationOptions[0].ResourceRecord.[Name,Value]' --output text)
  if [ -n "$ZONE_ID" ]; then
    aws route53 change-resource-record-sets --hosted-zone-id "$ZONE_ID" --change-batch "{
      \"Changes\": [{\"Action\": \"UPSERT\", \"ResourceRecordSet\": {
        \"Name\": \"$VNAME\", \"Type\": \"CNAME\", \"TTL\": 300,
        \"ResourceRecords\": [{\"Value\": \"$VVALUE\"}]}}]}" >/dev/null
    echo "Added the validation record to Route 53."
  else
    cat <<MSG

  Add this DNS record at your DNS provider for $PARENT_DOMAIN, then come back:

    Type:  CNAME
    Name:  $VNAME
    Value: $VVALUE

  (Some providers want the name without the trailing ".$PARENT_DOMAIN.")
MSG
  fi
  say "Waiting for the certificate to be issued (usually a few minutes; Ctrl-C is safe, just re-run later)"
  until aws_ acm wait certificate-validated --certificate-arn "$CERT_ARN" 2>/dev/null; do
    echo "  still waiting…"
  done
fi
echo "Certificate is issued."

# --------------------------------------------------------------- 3. GitHub settings
say "GitHub repository settings"
cat <<MSG
The deploy workflow reads these from $REPO (Settings > Secrets and variables > Actions):

  Variables:
    AWS_ROLE_ARN     = $ROLE_ARN
    CERTIFICATE_ARN  = $CERT_ARN
    HOSTED_ZONE_ID   = ${ZONE_ID:-(leave unset: DNS is not in Route 53)}
    DOMAIN_NAME      = $DOMAIN

  Secrets (optional, recommended):
    ACCESS_CODE      = a passphrase people must enter to download (keeps strangers
                       from running up your bill)
    YTDLP_PROXY      = a proxy URL, only if YouTube blocks the AWS servers
MSG

if command -v gh >/dev/null && gh auth status >/dev/null 2>&1; then
  printf '\nSet the variables now with the GitHub CLI? [Y/n] '
  read -r ans
  if [ "${ans:-y}" != "n" ] && [ "${ans:-y}" != "N" ]; then
    gh variable set AWS_ROLE_ARN --repo "$REPO" --body "$ROLE_ARN"
    gh variable set CERTIFICATE_ARN --repo "$REPO" --body "$CERT_ARN"
    gh variable set DOMAIN_NAME --repo "$REPO" --body "$DOMAIN"
    [ -n "$ZONE_ID" ] && gh variable set HOSTED_ZONE_ID --repo "$REPO" --body "$ZONE_ID"
    printf 'Set an ACCESS_CODE secret now? Type one (or press Enter to skip): '
    read -r code
    [ -n "$code" ] && gh secret set ACCESS_CODE --repo "$REPO" --body "$code"
    echo "Done."
  fi
fi

say "Next steps"
cat <<MSG
1. Merge the pull request into main (or run the "Deploy" workflow by hand).
   The first deploy takes ~5-10 minutes, mostly CloudFront.
MSG
if [ -z "$ZONE_ID" ]; then
cat <<MSG
2. After it finishes, run:  ./scripts/dns-target.sh
   and add the CNAME it prints at your DNS provider ($DOMAIN -> the CloudFront domain).
MSG
else
  echo "2. Nothing else: the deploy creates the $DOMAIN DNS record in Route 53."
fi
