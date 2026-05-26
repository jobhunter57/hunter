#!/usr/bin/env bash
# =====================================================================
#  RoleDar local run script (macOS / Linux)
# =====================================================================
#  You normally don't edit this file. Put your email settings in
#  local-secrets.sh instead. To schedule it, see the README section
#  "Setting up a local schedule."
# =====================================================================

# Always run from the folder this script lives in, so the scraper finds
# companies.yaml and friends no matter where it was launched from.
cd "$(dirname "$0")" || exit 1

# Load email settings if the user filled them in.
if [ -f "./local-secrets.sh" ]; then
  # shellcheck disable=SC1091
  . "./local-secrets.sh"
fi

# Decide whether email is really configured. A file copied from the template
# but not yet edited still holds the "re_..." / "you@example.com" placeholders;
# treat those (and blanks) as "off" so we don't hand junk to Resend.
if [ -z "$RESEND_API_KEY" ] || [ -z "$NOTIFY_EMAIL" ] || [ -z "$FROM_EMAIL" ] \
   || [ "$RESEND_API_KEY" = "re_..." ] || [ "$NOTIFY_EMAIL" = "you@example.com" ]; then
  # Clear them so scrape.py / notify.py see no email config and skip cleanly.
  unset RESEND_API_KEY NOTIFY_EMAIL FROM_EMAIL
  echo "[$(date)] Email is off (settings blank or still the template defaults) — writing CSV reports only."
else
  echo "[$(date)] Email settings found — will email $NOTIFY_EMAIL."
fi

# Run the scraper using the virtualenv's Python.
./.venv/bin/python scrape.py
