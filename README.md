# Camera Tunnel Control Plane

Team-scoped dashboard for reaching cameras that live on a restricted network:
dial a VPN, open SSH forwards, preview live in the browser, record RTSP and HLS
side by side — without a dropped link costing the whole recording.

- **Architecture:** https://claude.ai/code/artifact/0c411c17-f05f-49b5-9dec-82e45e49c51a
- **Deferred work and how we'd implement it:** [ROADMAP.md](ROADMAP.md)
- **Original requirements:** [camera.MD](camera.MD)
- **Storage runbook:** [deploy/versity-setup.md](deploy/versity-setup.md)

## Status

Phase 1, in progress. What exists and is tested:

| Area | State |
|---|---|
| VPN drivers (Fortinet, GlobalProtect/Palo Alto, WireGuard, direct) | done, tested against real client output |
| Certificate-trust flow (gate 2) | done |
| Network namespace per profile + kill-switch routing | done |
| SSH ControlMaster tunnel manager + port pool | done |
| Eight-gate ladder with per-mode skipping | done |
| Schema (13 tables) and team scoping | done |
| Secrets (sealed local, Vault-ready interface) | done |
| Credential redaction | done |
| Storage on hosted Versity + retention/admission | done |
| Compose stack (Postgres, Redis, MediaMTX, api, agent) | done |
| REST/WebSocket API surface | done |
| Recorder worker (segments, redial ladder, gaps.json) | done |
| Retention sweep and admission accounting | done |
| Dashboard frontend | done |
| Connect dispatched from the API to the agent | done |
| RTSP + HLS side-by-side compare view | done |

## Getting started

```bash
cp .env.example .env
# Fill in AWS_SECRET_ACCESS_KEY for s3.example.com — see deploy/versity-setup.md
# Generate the sealing key and fill in SECRETS_KEY:
python3 -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())"

make up        # bring up the whole stack
make test      # 145 tests, no network required
make api       # run the API locally against compose infra
make agent     # run the recorder against the same infra
```

`make help` lists the rest.

## How it fits together

Four modes of reaching a camera, declared once on a **connection profile**:

| Mode | Path | Used when |
|---|---|---|
| `direct` | app → camera | the app host already sits on the camera network |
| `vpn_only` | app → VPN → camera | the VPN routes to cameras with no VM in between |
| `jump_only` | app → jump VM → camera | already on the network, cameras behind the VM |
| `vpn_jump` | app → VPN → jump VM → camera | off-network, cameras behind the VM |

Everything downstream reads the profile and skips the hops that are not there.
The gate ladder reports skipped rungs rather than omitting them, so "we didn't
need a VPN" is distinguishable from "the VPN check never ran".

### The seams that matter

Three interfaces carry the design. Changing what is behind them should never
reach the rest of the system:

- **`VpnDriver`** (`app/net/vpn/base.py`) — four methods. A FortiClient build
  that satisfies endpoint posture is a fifth implementation, not a rewrite.
  ROADMAP entry 1 depends on this staying true.
- **`SecretsBackend`** (`app/security/secrets.py`) — three methods. Company
  Vault is a second implementation plus a migration script. ROADMAP entry 3.
- **`Runner`** (`app/net/runner.py`) — decides whether a command runs plainly or
  inside a VPN's network namespace. Every network-touching component takes one,
  which is also why the whole stack can be tested without a network.
- **`SourcePath`** (`app/recorder/session.py`) — four methods: open the path,
  say which hop died, close it. The recorder has no reconnection logic of its
  own; it reopens the path and starts the next run. A scheduled recording or a
  second reachability mode plugs in here.
- **`ConnectionGateway`** (`app/services/gateway.py`) — the four operations that
  touch the camera network, satisfied either by `ConnectionService` directly or
  by a request to the agent. Chosen once at startup, so no route knows which
  deployment it is in.

### Things that will bite if you change them carelessly

- **RTSP must be pinned to TCP.** `ssh -L` forwards TCP only; UDP RTSP through a
  tunnel connects and then delivers nothing at all.
- **Loopback comes up down inside a new namespace.** `ssh -L` binds `127.0.0.1`
  and ffmpeg dials it, so a namespace without `lo` up forwards nothing and says
  nothing about why.
- **The control route is deliberately not a default route.** If it were, a
  dropped VPN would silently reroute camera traffic onto the host network
  instead of failing. That ordering is the kill-switch.
- **`ExitOnForwardFailure=yes` is not optional.** Without it ssh reports success
  and forwards nothing.
- **Versity is path-style.** Virtual-host addressing resolves
  `cam-recordings.s3.example.com`, which does not exist, and fails like an outage.
- **Versity has no lifecycle rules and no bucket quotas.** The retention sweep
  and the pre-recording admission check are the only ceiling there is.
- **Uploads still do not run inside a VPN namespace**, even though storage is on
  our own network and a route would work. An upload started in there dies with
  the tunnel and with the namespace, and a customer VPN advertising `10.0.0.0/8`
  will collide with a storage host at `10.x.x.x`. Recorders write to the work
  volume; a shipper outside every namespace uploads. `CONTROL_CIDRS` exists for
  the cases that genuinely do need in-namespace reach.
- **The FortiClient GUI shows SHA-1; `openfortivpn` pins SHA-256.** Always take
  the digest from the client's own output, never from what the GUI displayed.
- **Segments are MPEG-TS, not MP4.** An MP4 is unreadable until its `moov` atom
  is written on clean exit — exactly what does not happen when a tunnel dies
  mid-write. The session is joined into an MP4 at the end, where there is a
  clean exit to rely on.
- **The recording deadline is wall clock.** An outage shortens the file and is
  recorded as a gap; it does not extend the session. Extending it would hand
  back footage of a different five minutes and double the storage the admission
  check was sized against.
- **The compare view's transport is wall clock too, and that is not cosmetic.**
  A session file is a concatenation of what was captured, so after a fifteen
  second outage everything sits fifteen seconds earlier in the file than it
  happened in the world. Seeking both players to the same media position
  compares frames minutes apart and looks exactly like the inference being
  wrong. `app/services/playback.py` maps a moment to each feed's own position;
  `frontend/src/lib/playback.ts` mirrors it.
- **Network namespaces cannot be shared between containers.** The agent is the
  only process that can dial, forward or record, which is why it is the only one
  with `NET_ADMIN`, and why `CONNECT_MODE=agent` sends the API's connect,
  disconnect, trust and camera-test straight to it over Redis. Unset (the
  default) runs them in process, which is what `make api` and the tests use.

## Layout

```
backend/app/
  config.py enums.py db.py models.py     schema, settings, team scoping
  security/  secrets.py redaction.py     sealed store; credential-safe URLs
  net/       runner.py netns.py          the exec seam; per-profile namespaces
             ssh.py ports.py             ControlMaster forwards; port leases
             vpn/                        four drivers behind one interface
  gates/     ladder.py probes.py         the eight gates
  api/       auth.py                     argon2 + signed session cookies
  storage/   client.py keys.py           Versity S3 gateway; team-scoped keys
             retention.py                admission check + oldest-first sweep
deploy/                                  Dockerfiles, MediaMTX config, storage runbook
```
