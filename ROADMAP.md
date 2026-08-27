# ROADMAP — deferred work and how we'd implement it

This file is the standing record of things we have **deliberately deferred**. Each entry
says what the thing is, what would trigger us needing it, how we'd implement it, and what
would have to change in code that already exists.

## How to use this file

**If you are an agent picking up a task from here:** read the entry in full before touching
code. The "What changes" section is the important part — it names the seams the current
design already has, so most of these should be additive rather than rewrites. If you find
the seam no longer exists, say so instead of forcing the change.

**If you are adding an entry:** keep the same five headings. An entry without a "What
changes" section is a wish, not a plan.

**When something ships:** change its status to `DONE`, add the date and a one-line pointer
to where it lives, and leave the entry in place. Delete an entry only when it is `DROPPED`
and we know why.

### Status legend

| Status | Meaning |
|---|---|
| `PLANNED` | Agreed we'll need it eventually. Not started. |
| `NEXT` | Queued for the current or following cycle. |
| `IN PROGRESS` | Someone is on it. Name them in the entry. |
| `DONE` | Shipped. Keep the entry, add date + location. |
| `DROPPED` | Decided against. Keep the reasoning. |

---

## 1. VPN host-check / endpoint posture enforcement

**Status:** `PLANNED` — not required today

### Context

Today we connect with `openfortivpn`, a headless SSL-VPN client. It authenticates with
username, password and gateway, and that is all our gateway currently asks for. Confirmed
27 Aug 2026: no endpoint posture check, no MFA. The only interactive step is a certificate
trust prompt, which we handle in-product (see the certificate-trust gate in the
architecture doc — that one is in scope now, not deferred).

### Trigger — when we'd need this

Any of:

- Infra enables FortiClient EMS with ZTNA tags or a compliance profile on our VPN group.
- The gateway starts rejecting `openfortivpn` with a posture or registration error.
- The org moves to ZTNA and publishes the camera network as ZTNA applications.

The symptom will show up at **gate 1 (VPN dial)** as an auth success followed by an
immediate disconnect, or an explicit `endpoint compliance` / `registration required`
error in the client log. That is the tell — do not chase it as a credentials bug.

### How we'd implement it

Three options, cheapest first.

**Option A — FortiClient Linux CLI as an alternate driver.** FortiClient ships a Linux
build with a CLI (`/opt/forticlient/fortivpn`) that can register to EMS and satisfy
posture. We add a `forticlient` driver alongside `openfortivpn`. Cost: it wants a host
install rather than a clean container, and EMS registration is a per-machine step that
infra has to do once on the office host. This is the option to try first.

**Option B — remote VPN agent on a posture-compliant host.** If the compliant machine
cannot be the app host (e.g. posture requires Windows), we stand up a small agent process
on that machine exposing dial / health / hangup over a local API, and our tunnel manager
reaches the camera network by treating that host as the SSH jump. The boundary crossing
moves off our box entirely. Cost: one more machine to own, and the health signal becomes a
network call rather than a namespace check.

**Option C — ZTNA access proxy, no tunnel at all.** If the org publishes cameras as ZTNA
applications, we connect through the access proxy and the whole VPN + SSH layer collapses
into an HTTPS client with a client certificate. Cheapest to run, entirely out of our hands
to enable. Worth asking infra about before building A or B.

### What changes

Very little, if the seam holds. The design has a `VpnDriver` interface with exactly three
operations:

```
dial(profile)  -> Connection      # raises TrustPromptRequired / AuthFailed / PostureFailed
health()       -> Status          # called on the 30 s heartbeat
hangup()       -> None
```

Implementations today: `openfortivpn`, `openconnect` (covers GlobalProtect and Palo Alto),
`wireguard`, `none` (direct mode). Host-check becomes a **fourth implementation**, not a
change to the control plane, the gate ladder, or the recorder.

What genuinely changes per option:

- **A:** new driver; the VPN container needs a host-installed binary bind-mounted in, or the
  driver runs on the host and the namespace is created by us rather than by the container
  runtime. Gate 1's failure taxonomy grows a `PostureFailed` case with its own user-facing
  message.
- **B:** new driver that speaks HTTP to the remote agent; the network namespace trick stops
  applying to that profile, so the kill-switch guarantee has to be re-established on the
  remote host or dropped for that profile with a note in the UI.
- **C:** the connection profile grows a `ztna` mode where both the VPN and SSH hops are
  absent — which the reachability-mode model already supports.

**Do not** let a posture requirement leak into the recorder, the gate ladder's later rungs,
or the camera model. If it does, the driver seam was drawn in the wrong place.

---

## 2. Multi-factor authentication on VPN dial

**Status:** `PLANNED` — gateway does not require it today

### Context

Current gateway asks for username and password only. Confirmed 27 Aug 2026.

### Trigger

Infra enables FortiToken, a push approval, or TOTP on the VPN group. Symptom: dial hangs
waiting on stdin, or returns a challenge prompt we don't answer.

### How we'd implement it

Two shapes, and they differ in what we can promise about auto-redial:

- **TOTP with a stored seed.** We seal the seed on the profile and generate the code
  server-side at dial time. Unattended redial still works. This is the one to push for if
  we get a choice, because it preserves the whole resilience story.
- **Push or user-entered code.** The dial blocks on a challenge. The VPN agent publishes a
  `challenge_pending` state, the dashboard prompts the owner of the profile, and the answer
  is passed back through Redis. Auto-redial after a network drop can no longer be silent —
  a drop means a human re-approves. The UI has to stop promising otherwise.

### What changes

- `VpnDriver.dial` gains a challenge callback, and `TrustPromptRequired` becomes one case of
  a general `InteractionRequired` — the certificate-trust flow already establishes this
  pattern, so MFA reuses its plumbing end to end.
- The supervisor's redial ladder needs a `requires_interaction` branch that stops retrying
  and waits rather than burning attempts against a gateway that will never answer.
- Recording sessions in flight need a policy: hold the segments and wait for re-approval, or
  finalize early. Default to holding for up to the session's remaining duration.

---

## 3. Company Vault for secrets

**Status:** `PLANNED` — storing locally for now, by decision

### Context

We hold VPN credentials, SSH credentials and camera credentials. Today these live in
libsodium-sealed Postgres columns with the key supplied as a Docker secret. The org runs a
Vault used company-wide; we chose not to depend on it for the first version.

### Trigger

Any of: the app moves out of the office host, a second deployment appears, an audit asks
where the credentials live, or the key-rotation question comes up and nobody wants to
answer it manually.

### How we'd implement it

Vault AppRole for the app, KV v2 for the static credentials, and Vault's own transit engine
for the sealing key so we never hold plaintext key material. Path convention:
`kv/cam-dashboard/<team>/<profile-id>`, which keeps the team boundary intact inside Vault.

### What changes

Only the secrets backend. The design puts every read and write behind:

```
SecretsBackend.put(ref, value) / get(ref) -> value / delete(ref)
```

Application code stores an opaque `ref`, never a ciphertext, so the local sealed-column
implementation and a Vault implementation are interchangeable. Migration is a one-shot
script that reads through the old backend and writes through the new one.

The rule that matters more than the backend: **credentials are never rendered back to the
UI and never reach a log line.** RTSP URLs get redacted at the point of construction, not at
the point of logging. If that holds, swapping backends is a weekend, not a project.

---

## 4. RTSP + HLS side-by-side comparison for QA

**Status:** `NEXT` — the reason the dual-source model exists

### Context

QA needs to watch the raw camera feed and the HLS feed of the same camera **at the same
time**, to check the inferred/annotated output against the actual video. The HLS stream is
produced elsewhere and handed to us as a directly accessible `.m3u8` URL — we do not produce
it and annotation is explicitly out of scope.

### Trigger

Straight after Phase 1 lands.

### How we'd implement it

- A camera carries up to two sources: an RTSP source and an optional HLS source, each with
  its own reachability (the HLS URL is often directly reachable while RTSP needs the tunnel).
- A recording session fans out to both sources concurrently, one FFmpeg process each,
  writing into the same session directory under `raw/` and `hls/`.
- Playback is a split view with a single shared transport control. Alignment is best-effort
  by wall-clock start time; expect the HLS side to lag by its segment duration.

### What changes

Nothing structural — the camera model and the session directory layout are already built for
two sources. The work is the compare UI and the shared scrubber.

### Known problem to solve when we build it

Frame-accurate alignment. HLS segment boundaries and RTSP timestamps will not line up, and
the inference pipeline adds its own latency. Decide early whether QA needs true frame
alignment (expensive — requires a common timestamp source, probably burned-in or embedded
via RTCP sender reports) or whether "within a second or two, labelled" is enough. Ask QA
before building; do not assume the expensive answer.

---

## 5. Producing the annotated stream ourselves

**Status:** `DROPPED` for now — explicitly out of scope

Annotation is produced by another system. We consume its HLS output. If this ever comes back
it is a GPU-shaped project of its own (decode → inference → overlay → encode) and should be
scoped separately rather than grown inside this dashboard. Keep the raw recording path
untouched by it whatever happens — the archive must never depend on the inference pipeline
being healthy.

---

## 6. Scheduled and unattended recording

**Status:** `PLANNED`

### Context

Today recordings are started by a person clicking a camera. `camera.MD` mentions a job that
can be run manually to work out which cameras to extract.

### Trigger

Someone asks for a recording to happen overnight, or on a repeating schedule.

### How we'd implement it

The scheduler already exists (arq). A schedule is a stored row: team, cameras, duration,
cron expression, retention override. The job enqueues exactly the same recording intent a
human click produces.

### What changes

- Unattended runs make MFA and certificate prompts blocking failures rather than
  inconveniences — see entries 1 and 2. A profile that can't dial without a human should be
  marked as ineligible for scheduling, in the UI, at schedule-creation time.
- Retention pressure changes shape: a nightly job can fill 100 GB quietly. The retention
  guard needs to refuse to *start* a scheduled run it can't fit, not discover it mid-write.

---

## 7. Per-camera fault isolation on the SSH layer

**Status:** `PLANNED` — deliberately not doing this yet

### Context

We use one multiplexed SSH master per jump host, with forwards added at runtime via
`ssh -O forward`. One dropped link takes every forward on that host down together.

### Trigger

Operational evidence that a single flapping camera or forward is repeatedly taking down
healthy recordings on the same host. Not before — this is a real trade and the current side
of it is correct at our scale.

### How we'd implement it

Move from one master per host to a small pool of masters per host, sharding cameras across
them, so a drop costs a fraction of the forwards rather than all of them.

### What changes

The tunnel manager's port-lease table gains a `master_id`, and the supervisor tracks N
sockets per host instead of one. The recorder is unaffected — it only ever knows about
`127.0.0.1:<port>`.

---

## 8. Horizontal scale beyond one host

**Status:** `PLANNED` — not needed at 3 users / 15 streams

### Trigger

More concurrent streams than one machine's NIC, disk or CPU can take, or a second office
needing its own boundary crossing.

### How we'd implement it

Agents already communicate over a Redis bus rather than in-process calls, so the first move
is running the agent set on a second host with the same bus, and adding a `host` column to
the tunnel and recording tables so the scheduler can place work. Kubernetes only if we
outgrow that too — not before.

---

## 9. Org-wide S3 hardening

**Status:** `PLANNED` — depends on how the NAS bucket gets set up

### Context

MinIO on the NAS will be used org-wide, not just by this app.

### Trigger

The moment a second application writes to the same MinIO.

### What to do then

- Our own bucket, our own service account, a policy scoped to that bucket only. Never the
  root credentials, not even in the first version.
- Bucket versioning off (we write immutable objects, versioning just doubles the disk) but
  object-lock considered if recordings ever become evidence.
- Our 100 GB working cap is *our* discipline, not the bucket's size. Add a MinIO quota on
  the bucket too, so a bug in our retention job cannot eat the org's storage.
