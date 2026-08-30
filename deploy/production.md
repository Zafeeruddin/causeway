# Deploying on a second machine

This is the runbook for standing the whole thing up on a host you own, behind
nginx, on your own domain. It assumes you have already read `README.md` and
`deploy/versity-setup.md`.

Two things about this application shape the entire deployment and are worth
knowing before you start:

- **The agent needs kernel privileges the other services do not.** It creates a
  network namespace per connection profile, which is a mount, which needs
  `SYS_ADMIN` *and* an AppArmor exemption on Ubuntu. See the comment on the
  `agent` service in `docker-compose.yml` for why all three are required.
- **WebRTC does not go through nginx.** Live preview negotiates over HTTP (which
  nginx proxies) and then carries video over UDP directly to MediaMTX on port
  8189. A reverse proxy in front of the video port does not help and will not
  work. Plan the firewall accordingly.

---

## 1. What it costs to run

Measured on this deployment, 29 Aug 2026, 16-core host, against real cameras
(1080p15 HEVC) and a synthetic source of the same shape. CPU is given as a
percentage of **one** core.

### At rest — nothing connected, nothing streaming

| Service | CPU | Memory |
|---|---|---|
| api | 0.3% | 89 MB |
| agent | 0.2% | 85 MB |
| mediamtx | 3.2% | 57 MB |
| postgres | 0.04% | 32 MB |
| redis | 1.1% | 14 MB |
| **Total** | **~5% of one core** | **~275 MB** |

That is the floor. It does not grow with the number of cameras or profiles
configured, because nothing runs per camera until someone asks for something.

### Per connection profile, while connected

A dialled profile holds an `openfortivpn` process, its `pppd` child, one SSH
master per jump host, and a network namespace. Idle cost is **negligible CPU**
and roughly **10–15 MB** of memory in total. The namespace itself is free.

Profiles that are *configured* but not connected cost nothing at all.

### Per camera

**A configured camera costs nothing.** It is a database row. Cost arrives only
when someone previews it or records it.

| Workload | CPU | Memory |
|---|---|---|
| Preview, H.264 source (stream copy) | **3%** of one core | 46 MB |
| Preview, H.265 source (transcoded to H.264) | **55%** of one core | 222 MB |
| Recording, any codec (stream copy to disk) | **1%** of one core | 43 MB |

The gap between the first two rows is the whole reason `needs_transcode()`
exists: no mainstream browser decodes H.265 over WebRTC, so those sources and
only those are re-encoded. An H.264 estate previews essentially for free; an
H.265 estate costs about half a core per person watching.

**Two people watching one camera cost one stream, not two** — MediaMTX is in the
middle precisely so the camera only ever sees a single session.

### Sizing

For the design target of 15 concurrent streams:

| Estate | Worst case | Recommend |
|---|---|---|
| All H.264 | 15 × 3% ≈ 0.5 core | 2 cores, 4 GB |
| All H.265, previewed | 15 × 55% ≈ 8 cores | **8 cores, 8 GB** |
| All H.265, recorded only | 15 × 1% ≈ 0.2 core | 2 cores, 4 GB |

Recording is cheap; previewing H.265 is not. Size for how many people watch at
once, not for how many cameras exist.

### Storage and bandwidth

Real numbers from the cameras on this deployment:

- **~0.18 Mbit/s per camera** — 1.3 MB per minute, about **78 MB per camera per
  hour**. These are heavily compressed 1080p15 HEVC streams of near-static
  scenes; a busier scene or an H.264 camera will be several times this.
- Upload to the storage gateway ran at about **18 Mbit/s** (48.7 MB in 21.7 s),
  so a one-minute recording finalises in a second or two and an hour-long one in
  under a minute.

The application's own admission estimate is deliberately generous and will
predict far more than 78 MB/hour. It refuses recordings it cannot fit rather
than discovering the limit mid-write, so erring high is the correct direction —
but do not use it to plan disk.

**Work volume**: recordings are written to disk before upload and deleted only
once the object is in the bucket. Size the `work` volume for the largest
concurrent set of recordings you expect, plus anything stranded by a failed
upload. 20 GB is comfortable at this scale.

---

## 2. Prerequisites on the new host

- Ubuntu 22.04 or newer, Docker Engine with the compose plugin.
- The `ppp` kernel module and `/dev/ppp` present — `modprobe ppp_generic`, and
  add `ppp_generic` to `/etc/modules-load.d/` so it survives a reboot. Without
  it the VPN dial fails with a pppd error that does not mention the device.
- Ports **80** and **443** open to the internet (nginx), and **8189/udp** open
  to wherever your users are (WebRTC media). Nothing else needs to be public.
- A DNS `A` record for your domain pointing at the host.
- Network reachability from the host to the VPN gateway and to the storage
  gateway. Neither goes through the tunnel.

---

## 3. Bring the stack up

```bash
git clone <your remote> cam-dashboard && cd cam-dashboard
cp .env.example .env
```

Edit `.env`. The entries that must change for a new host:

| Key | Set it to |
|---|---|
| `POSTGRES_PASSWORD` | something generated, not the default |
| `APP_SECRET_KEY` | `openssl rand -base64 48` — sessions are signed with it |
| `SECRETS_KEY` | `openssl rand -base64 32` — **credentials are sealed with this; lose it and every stored VPN, SSH and camera password is unrecoverable** |
| `S3_ENDPOINT_URL`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | your storage gateway and its account |
| `PREVIEW_HOST` | the **public** hostname or IP browsers will reach, e.g. `cams.example.com` |
| `PREVIEW_PUBLIC_BASE` | **leave empty.** WebRTC signalling then stays on the dashboard's own origin at `/rtc`, which is what the nginx block below serves. Set it only if you expose MediaMTX to browsers directly instead. |
| `CONTROL_SUBNET` | only if 172.28.0.0/16 collides with something already on the host |

Then:

```bash
docker compose up -d --build
docker compose exec api cam init-db
docker compose exec api cam create-admin you@example.com
```

`create-admin` makes a **superadmin**: the account that owns the deployment and
can create teams. Everyone else is created from the Admin page.

Build and run the frontend:

```bash
cd frontend && npm ci && npm run build
```

Run it under systemd or pm2 on port 3000. Next proxies `/api` and `/rtc` itself
(see `next.config.ts`), so give the process the two origins it forwards to:

```
API_ORIGIN=http://127.0.0.1:8000
MEDIAMTX_ORIGIN=http://127.0.0.1:8889
```

The nginx block below routes `/api` and `/rtc` straight to the backends and
never reaches Next for those, so this matters mainly as a fallback and for a
run without nginx in front.

---

## 4. nginx and the domain

Install nginx and certbot, then get a certificate:

```bash
sudo apt install nginx certbot python3-certbot-nginx
sudo certbot --nginx -d cams.example.com
```

`/etc/nginx/sites-available/cam-dashboard`:

```nginx
server {
    listen 443 ssl http2;
    server_name cams.example.com;

    ssl_certificate     /etc/letsencrypt/live/cams.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/cams.example.com/privkey.pem;

    # Recordings are downloaded straight from the storage gateway by presigned
    # URL, so nothing large flows through here. CSV import is the only upload.
    client_max_body_size 4m;

    # The dashboard.
    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # The API.
    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # The gate ladder streams results as they land, and a connect walks
        # eight rungs with a 45s VPN dial in the middle. The 60s default cuts
        # it off mid-ladder and the dashboard reports a failure that did not
        # happen.
        proxy_read_timeout 300s;
    }

    # The live event socket. Without these two headers it is a 400, not a
    # websocket, and every page silently stops updating.
    location /api/ws {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade    $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host       $host;
        proxy_read_timeout 3600s;
    }

    # WHEP: WebRTC *signalling* only. The video itself does not come through
    # here -- see the note at the top of this file.
    location /rtc/ {
        proxy_pass http://127.0.0.1:8889/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
    }
}

server {
    listen 80;
    server_name cams.example.com;
    return 301 https://$host$request_uri;
}
```

```bash
sudo ln -s /etc/nginx/sites-available/cam-dashboard /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

### The one that will catch you

`PREVIEW_HOST` must be the address **browsers** dial, not the container's. MediaMTX
offers the browser whatever addresses it can see, which inside compose is a
172.28.x.x bridge address no machine outside can reach. Get this wrong and the
WebRTC session negotiates successfully and then video never starts, with nothing
logged anywhere. Set it, and open **8189/udp** to your users.

---

## 5. After it is up

- **Move off the root storage credential.** `deploy/versity-setup.md` has the
  commands. The app should own its bucket, not the whole gateway.
- **Back up `SECRETS_KEY`** somewhere other than the host. It is the only thing
  standing between the sealed columns and plaintext, and the only thing that can
  read them back.
- **`docker compose logs -f agent`** is where connection failures explain
  themselves. The gate that failed names the hop.
- There are no Alembic migrations yet (ROADMAP entry 10): the schema is created
  by `cam init-db`. Do not expect to carry data across a schema change until
  that lands.
