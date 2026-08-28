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

**Status:** `DONE` — 28 Aug 2026. `app/services/playback.py` and
`GET /api/recordings/{id}/comparison`; the view is
`frontend/src/app/(dash)/recordings/[id]/page.tsx`.

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

Nothing structural, and less than when this was written. As of 28 Aug 2026 the recorder
already fans out to every source of a camera concurrently, one ffmpeg each, into
`<recording>/rtsp/` and `<recording>/hls/` under the same session prefix, and each source
carries its own reachability (`camera_sources.uses_profile_path`). What is left is the
compare UI and the shared scrubber.

### The alignment question, and what we did about it

This entry said to ask QA whether they need true frame alignment before building, and not to
assume the expensive answer. **We could not ask, so we built the cheap answer and labelled
it.** If QA comes back needing frame accuracy, the expensive path is still open and nothing
here blocks it — it needs a common timestamp source (burned-in, or embedded via RTCP sender
reports), which is a change to what we record, not to how we play it back.

What shipped instead:

- Alignment is by wall clock, quoted to the user as **about two seconds** on the view itself.
  The spans are exact; what is not exact is that a span begins when ffmpeg started, not when
  the first frame landed, and RTSP and HLS do not buffer alike.
- The inference pipeline's own latency is unknowable to us, so it is a **manual trim** on the
  inferred feed, in tenths of a second, with the reason written next to it. QA nudges it once
  and it holds for the session.

The harder half turned out not to be latency at all. A session file is a *concatenation* of
what was captured, so a fifteen second outage is not fifteen seconds of black — it is absent,
and everything after it sits fifteen seconds earlier in the file than it happened in the
world. Two feeds that dropped at different times drift apart by exactly the outages they did
not share, and seeking both to the same media position compares frames minutes apart while
looking perfectly plausible. So the transport runs on wall-clock time and each feed converts
a moment into its own position through its own spans. A feed that missed the moment shows why
it missed it — the gap's cause — rather than showing something else.

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

## 9. Org-wide storage hardening

**Status:** `PLANNED` — partially addressed; the quota is the open item

### Context

Recordings live on `https://s3.example.com`, a hosted Versity Gateway shared across
the org. Versity speaks the S3 API but is not AWS S3, and two things it lacks
changed our design (see `deploy/versity-setup.md`):

- **No lifecycle rules.** Every deletion is ours to make. `app/storage/retention.py`
  is the only thing that ever removes an object.
- **No bucket quotas.** There is no server-side ceiling. The admission check
  before each recording is the only guard, and it depends on our own accounting
  being right.

The application already runs its own thresholds (60 / 90 / 98 GB) and refuses
recordings that will not fit rather than discovering the limit mid-write.

### Trigger

Any of: our accounting drifts from what the bucket actually holds; a second
application starts writing to `cam-recordings`; or the app is still running as
`storage-root` when someone asks who can reach the rest of the gateway.

### How we'd implement it

1. **Stop using the root credential.** Create a `cam-dashboard` account and make
   it the owner of `cam-recordings` — commands are in `deploy/versity-setup.md`.
   This is the cheapest item here and should not wait for the others.
2. **Ask for a filesystem quota on the bucket's backing directory.** Versity
   sits on a POSIX filesystem, so the operators can likely apply a quota there.
   That restores the outer safety net with no change on our side.
3. **Reconcile accounting against reality.** A periodic job that walks
   `ObjectStore.measure_prefix` per team and compares it with the
   `storage_objects` table. Drift means an upload that we recorded and the
   gateway dropped, or an object we deleted and forgot to unrecord — both are
   silent today.

### What changes

Item 1 is a `.env` change. Item 2 is entirely on the gateway side. Item 3 is a
new arq job plus a `storage_reconciliations` table; nothing in the recording or
retention path moves, because `measure_prefix` already exists for exactly this.

---

## 10. Alembic migrations

**Status:** `PLANNED` — `cam init-db` creates the schema for now, by decision

### Context

`app/cli.py` builds the schema straight from the models with `create_all`. The schema is new
and still churning weekly; a migration chain written now would be mostly rewrites of itself,
and every one of them would have to be reviewed. There is exactly one deployment and no data
worth preserving across a schema change.

### Trigger

The first of: someone needs to keep data across a schema change, a second deployment appears,
or the schema goes two cycles without a column moving.

### How we'd implement it

`alembic init`, autogenerate a baseline from the current models, and stamp the existing
database with it so no one has to rebuild. `make migrate` and `make revision` already exist
in the Makefile pointing at Alembic, so the workflow is in place before the chain is.

### What changes

`cam init-db` stops being the way the schema is created and becomes a dev shortcut, or goes.
Nothing else: the models are already the single definition, which is what makes autogenerate
usable at all. Note that `storage_objects.source_kind` is nullable — session-level objects
like `gaps.json` belong to no source — so the baseline must not tighten it.

---

## 11. Dispatching connect from the API to the agent

**Status:** `DONE` — 28 Aug 2026. `app/services/gateway.py` (the seam),
`app/services/commands.py` (the bus), `app/agent/commands.py` (the agent's side).

### Context

Network namespaces belong to a container. The agent creates them, holds the VPN clients and
the SSH masters, and runs every ffmpeg, which is why it is the only service with
`NET_ADMIN`. The API's `POST /api/profiles/{id}/connect` still runs `ConnectionService` in
the API container, where a namespace cannot be created and a forward opened by the agent
cannot be reached.

### What we did

Request/response over the Redis that already carries events. `CONNECT_MODE=agent` (set on
the api service in compose) swaps `ConnectionService` for `RemoteGateway`, which publishes on
`cam:commands` and waits on a per-command reply channel. The agent serves the four commands
with the same `ConnectionService` calls the API used to make itself, so there is one
implementation of connecting and two ways to reach it. Gate results still stream over the
team channel as they land; the reply carries the outcome, and the endpoint keeps its
synchronous shape.

Two details worth keeping:

- **Pub/sub, not a queue.** `PUBLISH` reports its subscriber count, so an API with no agent
  behind it answers 503 "the recorder agent is not running" immediately, rather than timing
  out two minutes later or queueing a connect that fires ten minutes after the person gave up.
- **The certificate pin is written by the agent.** The dial that reads it happens in a
  separate command, and the API request's transaction has not committed yet when that command
  starts — pinning it API-side would redial against the certificate the user was asked about.

### What changed

- `ConnectionGateway` has two implementations: `ConnectionService` itself (in process, and
  what the test suite uses) and `RemoteGateway`. Chosen once at startup, not per request.
- `app/services/commands.py` is a separate channel from the event fan-out, same Redis client.
- `/api/health` and `/api/capabilities` report the *agent's* namespace availability in agent
  mode, so "VPN profiles cannot work here" is visible before someone presses connect.
- Nothing in the recorder changed. `SourcePath.open` already reconnects through
  `ConnectionService` inside the agent, which owns it either way.

---

## 12. Authentication on the preview server

**Status:** `PLANNED` — path names are unguessable, which is not the same as access control

### Context

MediaMTX runs with no authentication configured, so every action it offers is anonymous.
Anyone who can reach port 8889 on the LAN and knows a path name can watch that stream. The
API on 9997 is anonymous too and is only out of reach because compose does not publish it —
one added port mapping and anyone can add, list or delete paths outright.

What stands between a stream and a stranger today is the path name. `app/services/preview.py`
names paths `preview/<16 random characters>` rather than by camera id, and deletes the path
when the publisher stops, so a name is neither guessable nor useful for long. But the name
travels in the WHEP URL the dashboard hands the browser, and a name that leaks — a shared
screenshot, a proxy log, devtools — is a stream anyone can open until the reaper takes it.
The dashboard itself is behind session auth and team scoping; the preview server is not
behind anything.

### Trigger

The first of: the preview port becoming reachable from anything wider than the office LAN,
a second team on the same deployment (unguessable is a weak answer to "can team A watch team
B's cameras"), or an audit asking who watched what — MediaMTX cannot answer that today
because it never learns who anyone is.

### How we'd implement it

MediaMTX has three `authMethod` values, and only one of them knows about our users:

- `internal` — a user list in `mediamtx.yml`, with per-action (`publish`, `read`, `api`)
  and per-path permissions. Cheapest, and worth doing on its own even if we go further:
  a `publish`-only user for the agent, an `api`-only user for path management, and no
  anonymous `read`. It cannot express "this person, this camera", because it does not know
  our people.
- `http` — MediaMTX POSTs each action to a URL of ours with the user, password, token, IP,
  action, path and protocol, and allows it on a 20x. This is the one that fits: the API
  already holds sessions, teams and cameras, so it can answer the question properly.
- `jwt` — MediaMTX pulls a JWKS and validates a token carrying a `mediamtx_permissions`
  claim. No callback per view, but it needs an identity server we do not have, and the
  permissions have to be minted before the path exists.

The plan is `internal` first, then `http`. A new unauthenticated-by-session endpoint —
`POST /api/internal/mediamtx-auth`, bound to the control network and nothing else — takes
MediaMTX's payload and decides:

1. `publish` and `api` are matched against a static credential the agent and the API hold,
   so only we can create paths and push to them.
2. `read` is matched against a per-preview token. `PreviewManager.start` already mints the
   path name; it would mint a viewing token alongside it, bound to the preview id, the user
   and the team, and store it on the `_Live` record it already keeps. The endpoint looks up
   the path in the manager and checks the token against it. Nothing new persists — previews
   are process-local and die with the process, which is already true of the paths themselves.
3. The browser sends it as the password of an `Authorization: Basic` header on the WHEP
   request, which is how MediaMTX takes credentials for WebRTC and HLS.

In agent mode the manager lives in the agent and the endpoint lives in the API, so the lookup
in step 2 goes over the command bus that `app/services/commands.py` already carries.

### What changes

- `deploy/mediamtx.yml` gains `authInternalUsers` (and later `authMethod: http` plus
  `authHTTPAddress`), with the credentials injected as `MTX_*` env from compose rather than
  written into the file.
- `MEDIAMTX_PUBLISH_URL` and `MEDIAMTX_API_URL` carry credentials, which means they become
  secrets: `app/security/redaction.py` already exists for exactly this and the publish URL is
  built in `_publish_target`, so there is one place to change.
- `PreviewOut` grows a token field beside `whep_url`, and the frontend player attaches it to
  the WHEP request instead of dialling the URL bare.
- The reaper is unaffected. It asks MediaMTX over the API, which by then is an authenticated
  call like any other.
- Nothing in the recorder moves. Recording never touches MediaMTX.
