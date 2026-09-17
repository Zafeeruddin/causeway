#!/usr/bin/env bash
#
# Redeploy the API and the dashboard.
#
# Two things this does that a bare `docker compose up -d` does not:
#
#   * It refuses while a recording is in flight. A recording is a live ffmpeg
#     process writing segments to the work volume; replacing a container
#     underneath it strands them part-uploaded, and the recording is lost
#     rather than merely interrupted.
#   * It raises the maintenance screen first and lowers it only once the new
#     containers answer. Without it, whoever is signed in meets requests that
#     fail for no stated reason, and whoever arrives meets a blank page.
#
# The agent is deliberately not in the default list. Replacing it kills the VPN
# link, the network namespace and the SSH master to the jump host, and every
# camera behind a tunnel goes with them until an operator reconnects each
# profile by hand. Deploy it on purpose, with section 5b of production.md open.
#
# Usage:  ./redeploy.sh                     # api and web
#         SERVICES="api" ./redeploy.sh      # just the API
#         NOTE="Back by 14:00" ./redeploy.sh
set -euo pipefail

cd "$(dirname "$0")"

COMPOSE=${COMPOSE:-compose.prod.yml}
ENV_FILE=${ENV_FILE:-.env}
SERVICES=${SERVICES:-"api web"}
NOTE=${NOTE:-"Causeway is being updated. This usually takes under a minute."}
#: How long to wait for the new API to answer before giving up, in 2s steps.
TRIES=${TRIES:-30}

c() { docker compose -f "$COMPOSE" --env-file "$ENV_FILE" "$@"; }

# ---- 1. refuse if anything is recording ---------------------------------
# Defaults to "1" when the query itself fails: a guard that cannot read the
# database must not conclude that nothing is running.
inflight=$(c exec -T postgres psql -U cam -d cam -At -c \
  "select count(*) from recordings where state in ('queued','recording','recovering','finalizing');" \
  2>/dev/null || echo 1)

if [ "${inflight:-1}" != "0" ]; then
  echo "refusing to deploy: ${inflight} recording(s) in flight." >&2
  echo "Replacing a container now strands their segments mid-upload." >&2
  echo "Wait for them to finish, or cancel them from the Recordings page." >&2
  exit 1
fi
echo "guard: nothing is recording"

# ---- 2. raise the maintenance screen ------------------------------------
# Redis is not among the services being replaced, so the flag outlives the
# swap it is describing -- including the moment the api container is gone.
raised=0
if c exec -T api cam maintenance on --note "$NOTE"; then
  raised=1
else
  # The first deploy of this feature replaces an API that does not have the
  # command yet, and so does a rollback to one. Neither is a reason to refuse
  # the deploy; both are a reason to say the screen is not up.
  echo "note: the running API has no maintenance command, so this deploy is" >&2
  echo "unannounced. The next one will not be." >&2
fi

# ---- 3. swap ------------------------------------------------------------
# shellcheck disable=SC2086 # SERVICES is a deliberate word list
c pull $SERVICES
# shellcheck disable=SC2086
c up -d --no-deps $SERVICES

# ---- 4. wait for the new API, then lower the screen ---------------------
ready=0
for _ in $(seq 1 "$TRIES"); do
  if c exec -T api python -c \
      "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health', timeout=3)" \
      >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 2
done

if [ "$ready" != "1" ]; then
  echo "the new API did not answer; leaving the maintenance screen up." >&2
  echo "Investigate with: docker compose -f $COMPOSE logs --tail=50 api" >&2
  exit 1
fi

if [ "$raised" = "1" ]; then
  c exec -T api cam maintenance off
fi
echo "deployed: $SERVICES"
