# Camera Tunnel Control Plane

Team-scoped dashboard for reaching cameras that live on a restricted network:
dial a VPN, open SSH forwards, preview live in the browser, record RTSP and HLS
side by side — without a dropped link costing the whole recording.

- **Architecture:** https://claude.ai/code/artifact/0c411c17-f05f-49b5-9dec-82e45e49c51a
- **Deferred work and how we'd implement it:** [ROADMAP.md](ROADMAP.md)
- **Original requirements:** [camera.MD](camera.MD)

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
| Compose stack (Postgres, Redis, MinIO, MediaMTX, api, agent) | done |
| REST/WebSocket API surface | next |
| Recorder worker (segments, supervisor, gaps.json) | next |
| Dashboard frontend | next |

## Getting started

```bash
cp .env.example .env
# Generate the sealing key and fill in SECRETS_KEY:
python3 -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())"
# Set MINIO_ROOT_PASSWORD and S3_SECRET_KEY too — compose refuses to start without them.

make up        # bring up the whole stack
make test      # 41 tests, no network required
make api       # run the API locally against compose infra
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
- **The FortiClient GUI shows SHA-1; `openfortivpn` pins SHA-256.** Always take
  the digest from the client's own output, never from what the GUI displayed.

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
deploy/                                  Dockerfiles, MinIO and MediaMTX config
```
