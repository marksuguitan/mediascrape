# mediascrape

A small web app that downloads YouTube videos, hosted on AWS at
**https://mediascrape.hypersynaptic.com**. Paste a link, pick a quality, get an
MP4 (or M4A audio). Merging a PR into `main` deploys it automatically.

## How it works

```
browser ──► CloudFront (mediascrape.hypersynaptic.com)
              ├─ /          ─► S3 site bucket (index.html, app.js, style.css)
              ├─ /api/*     ─► Lambda function URL (mediascrape-api)
              └─ /files/*   ─► S3 downloads bucket (files expire after 1 day)

POST /api/download ─► Lambda queues a job and re-invokes itself asynchronously
                      ─► yt-dlp + ffmpeg download and merge into /tmp
                      ─► upload to the downloads bucket ─► status "done"
GET  /api/status   ◄── the page polls every 2 s, then saves /files/<job>/<id>.mp4
```

| Path | What it is |
| --- | --- |
| `site/` | The static page (plain HTML/CSS/JS, no build step) |
| `backend/app.py` | The Lambda: HTTP API plus the download worker |
| `backend/Dockerfile` | Lambda container image: Python 3.12, yt-dlp, Deno, static ffmpeg |
| `infra/app.yml` | CloudFormation for the app (buckets, CloudFront, Lambda, DNS) |
| `infra/bootstrap.yml` | One-time CloudFormation: GitHub OIDC deploy role and ECR repository |
| `scripts/bootstrap.sh` | Run once on your laptop to set up AWS and the certificate |
| `.github/workflows/deploy.yml` | Deploys on every push to `main`, plus a weekly rebuild |
| `.github/workflows/ci.yml` | Tests, CloudFormation lint and an image build on every PR |

## One-time setup

You need the AWS CLI logged in to your account with admin rights.

1. **Run the bootstrap script** from the repo root:

   ```bash
   ./scripts/bootstrap.sh
   ```

   It creates the GitHub deploy role (OIDC, so no AWS keys are stored in GitHub)
   and the image repository, requests the HTTPS certificate for
   `mediascrape.hypersynaptic.com` in `us-east-1`, and validates it. If
   `hypersynaptic.com` is in Route 53 it adds the DNS records itself; if not, it
   prints the CNAME to add at your DNS provider and waits for it.

2. **Set the GitHub repo variables** it prints (Settings → Secrets and variables
   → Actions). If the GitHub CLI (`gh`) is logged in, the script offers to do
   this for you.

   | Name | Kind | Value |
   | --- | --- | --- |
   | `AWS_ROLE_ARN` | variable | printed by the script |
   | `CERTIFICATE_ARN` | variable | printed by the script |
   | `HOSTED_ZONE_ID` | variable | only if the domain is in Route 53 |
   | `DOMAIN_NAME` | variable | `mediascrape.hypersynaptic.com` (the default) |
   | `ACCESS_CODE` | secret | recommended: a passphrase the page asks for before downloading |
   | `YTDLP_PROXY` | secret | optional, see "When YouTube blocks AWS" below |

3. **Merge into `main`** (or run the *Deploy* workflow by hand). The first
   deploy takes 5–10 minutes, mostly CloudFront.

4. **DNS, only if the domain is not in Route 53:** run `./scripts/dns-target.sh`
   and add the CNAME it prints (`mediascrape` → `dxxxx.cloudfront.net`).

Until `AWS_ROLE_ARN` is set, the Deploy workflow skips itself instead of failing.

## Deploys

- Every push to `main` (i.e. every merged PR) runs tests, builds the Lambda
  image, pushes it to ECR, updates the CloudFormation stack and uploads `site/`.
- A weekly scheduled run rebuilds the image so it always has the newest
  yt-dlp. You can also run it any time from the Actions tab.
- Pull requests run unit tests, `cfn-lint` and a Docker build.

## Cost

Everything is pay-per-use with nothing running when idle. For personal use it
should be free or a few cents a month:

| Piece | Cost |
| --- | --- |
| CloudFront | Always-free tier: 1 TB transfer and 10M requests a month |
| Lambda | Always-free tier: 400,000 GB-seconds a month (thousands of downloads at 1 GB memory) |
| S3 | Pennies; downloaded files are deleted after a day |
| ECR | About $0.03/month (3 images kept) |
| ACM certificate | Free |
| Route 53 | $0.50/month per hosted zone, only if you already use it |

Abuse is the real cost risk, because the endpoint is public. Set the
`ACCESS_CODE` secret, and consider a billing alarm in the AWS console. Limits
are also built in: videos over 2 hours or files over 1.5 GB are refused (change
`MaxDurationMinutes` / `MaxFilesizeMB` in `infra/app.yml`).

## When YouTube blocks AWS

YouTube often blocks requests from cloud data centres with "Sign in to confirm
you're not a bot". If downloads fail with that message, there are two fixes:

- **Cookies:** export `cookies.txt` from a browser logged into a **throwaway**
  Google account (YouTube can ban accounts used this way) with an extension
  such as "Get cookies.txt LOCALLY", then upload it:

  ```bash
  BUCKET=$(aws --region us-east-1 cloudformation describe-stacks --stack-name mediascrape-app \
    --query "Stacks[0].Outputs[?OutputKey=='DownloadsBucket'].OutputValue" --output text)
  aws s3 cp cookies.txt "s3://$BUCKET/config/cookies.txt"
  ```

  The bucket is private, and cookies are not exposed through CloudFront.
- **Proxy:** put a residential proxy URL (`http://user:pass@host:port`) in the
  `YTDLP_PROXY` secret and redeploy. This costs money with most providers.

## Local development

```bash
pip install -r backend/requirements-dev.txt
python -m pytest -q backend/tests
docker build -t mediascrape backend   # the Lambda image
```

The page is static: open `site/index.html` with any local server. It calls
`/api/*` on its own origin, so the buttons only work once deployed.

## A note on terms

Downloading from YouTube is against YouTube's Terms of Service except where
YouTube offers a download option, and downloading copyrighted videos without
permission may be illegal where you live. Use it for videos you own or have
the rights to.
