#!/usr/bin/env bash
# Build and publish the public static demo to S3.
#
#   ./scripts/build_site.sh
#
# ui/ is written for the local server, which has routes and a session. S3 has
# neither, so this produces a transformed copy in build/site/ rather than a second
# set of hand-maintained files:
#
#   * const API      -> the API Gateway base URL (S3 serves no /api/*)
#   * /app           -> app.html            (no server routing on a static site)
#   * Settings tab   -> removed             (a public write endpoint would let anyone
#                                            edit the agent's beliefs — reads only)
#   * login.html     -> not uploaded        (no session to gate)
set -euo pipefail

REGION="${REGION:-ap-south-1}"
SITE="${SITE:-freelance-guardian-demo-933036664603}"
API_NAME="${API_NAME:-freelance-guardian-api}"

cd "$(dirname "$0")/.."

# CI passes API_BASE directly so the deploy role needs no apigateway read permission.
if [ -z "${API_BASE:-}" ]; then
  API_ID="$(aws apigatewayv2 get-apis --region "$REGION" \
    --query "Items[?Name=='$API_NAME'].ApiId" --output text)"
  [ -n "$API_ID" ] && [ "$API_ID" != "None" ] || {
    echo "no API Gateway named $API_NAME in $REGION — create it, or set API_BASE"; exit 1; }
  API_BASE="https://$API_ID.execute-api.$REGION.amazonaws.com"
fi
echo "api   $API_BASE"

rm -rf build/site && mkdir -p build/site
cp ui/landing.html ui/app.html ui/theme.css ui/theme.js ui/logo.svg build/site/
# The local server generates this; a static bucket has to carry the file.
printf '%s' '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><rect width="16" height="16" fill="#F26522"/></svg>' \
  > build/site/favicon.svg

API_BASE="$API_BASE" python3 - <<'PY'
import os, re, pathlib

api = os.environ["API_BASE"]
site = pathlib.Path("build/site")

for name in ("landing.html", "app.html"):
    p = site / name
    s = p.read_text()
    s = s.replace("const API = '';", f"const API = '{api}';")
    s = s.replace('href="/app"', 'href="app.html"')
    s = s.replace('href="/logo.svg"', 'href="logo.svg"')
    s = s.replace('href="/theme.css"', 'href="theme.css"')
    s = s.replace('src="/theme.js"', 'src="theme.js"')
    s = s.replace('href="/"', 'href="landing.html"')
    s = s.replace('href="/app#/dashboard"', 'href="app.html#/dashboard"')
    p.write_text(s)

# Strip Settings from the app: the tab, the panel, and the route.
p = site / "app.html"
s = p.read_text()
s = s.replace('    <a href="#/settings" data-tab="settings">Settings</a>\n', "")
s = re.sub(r"<!-- ═+ SETTINGS ═+ -->.*?\n</div>\n\n</main>", "\n</main>", s, flags=re.S)
s = s.replace("const TABS = ['review', 'dashboard', 'settings'];",
              "const TABS = ['review', 'dashboard'];   // Settings is local-only")
# loadSettings() and the write handlers have nothing to bind to now.
s = s.replace("  if (tab === 'settings') loadSettings();\n", "")
p.write_text(s)

leftovers = [t for t in ("#/settings", 'data-tab="settings"') if t in s]
assert not leftovers, f"Settings not fully removed: {leftovers}"
print("  transformed landing.html, app.html")
PY

if [ "${BUILD_ONLY:-}" = "1" ]; then
  echo "built build/site/ (BUILD_ONLY=1, not syncing)"
  exit 0
fi

echo "sync  s3://$SITE"
aws s3 sync build/site/ "s3://$SITE/" --delete --region "$REGION" \
  --cache-control 'no-cache' >/dev/null

if [ -n "${CLOUDFRONT_DIST:-}" ]; then
  echo "flush CloudFront $CLOUDFRONT_DIST"
  aws cloudfront create-invalidation --distribution-id "$CLOUDFRONT_DIST" \
    --paths '/*' --query 'Invalidation.Status' --output text
fi

echo
echo "demo  ${DEMO_URL:-http://$SITE.s3-website.$REGION.amazonaws.com}"
