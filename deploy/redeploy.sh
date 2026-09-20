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
#   * It fetches the images first, then raises the maintenance screen, and
#     lowers it once the new containers answer. Without the screen, whoever is
#     signed in meets requests that fail for no stated reason and whoever
#     arrives meets a blank page. Pulling before raising it matters just as
#     much: the pull is the slow, unpredictable step, and holding a screen up
#     for the length of it -- ten minutes, once, on a 200 KB/s registry -- is
#     an outage invented by the thing meant to explain one.
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
#: A ceiling on the pull. Harbor to this host has run at 200 KB/s, and an
#: unbounded pull is how a deploy hangs with nothing to show for it.
PULL_TIMEOUT=${PULL_TIMEOUT:-1800}

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

# ---- 2. fetch the images, before anyone is locked out -------------------
# Pulling is the slowest and least predictable step here -- this registry has
# served this host at 200 KB/s -- and none of it needs a maintenance screen.
# Fetching first means that by the time the screen goes up the images are
# already local and the swap is seconds. It also means a registry that is
# slow or down costs nothing: the deploy stops having changed nothing, and
# nobody ever saw a screen.
echo "pulling $SERVICES"
# `timeout` cannot call a shell function, so this spells the command out.
# shellcheck disable=SC2086 # SERVICES is a deliberate word list
if ! timeout "$PULL_TIMEOUT" docker compose -f "$COMPOSE" --env-file "$ENV_FILE" pull $SERVICES; then
  echo "the pull did not finish within ${PULL_TIMEOUT}s. Nothing was changed," >&2
  echo "and nobody saw a maintenance screen -- images are fetched before it." >&2
  exit 1
fi

# ---- 3. raise the maintenance screen ------------------------------------
# Redis is not among the services being replaced, so the flag outlives the
# swap it is describing -- including the moment the api container is gone.
raised=0
if c exec -T api cam maintenance on --note "$NOTE"; then
  raised=1
  # From here on the screen is up, so an interrupt must take it down again.
  # The one case that deliberately leaves it up is a new API that never
  # answers, handled below: a half-finished deploy should not look open.
  trap 'if [ "$raised" = "1" ]; then c exec -T api cam maintenance off >/dev/null 2>&1 || true; fi' INT TERM
else
  # The first deploy of this feature replaces an API that does not have the
  # command yet, and so does a rollback to one. Neither is a reason to refuse
  # the deploy; both are a reason to say the screen is not up.
  echo "note: the running API has no maintenance command, so this deploy is" >&2
  echo "unannounced. The next one will not be." >&2
fi

# ---- 4. swap ------------------------------------------------------------
# shellcheck disable=SC2086
c up -d --no-deps $SERVICES

# ---- 5. wait for the new API, then lower the screen ---------------------
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
