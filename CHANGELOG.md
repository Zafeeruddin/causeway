# Changelog

Notable changes, newest first. Dates are when the change reached a running
deployment, not when it was written.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the versions follow [semantic versioning](https://semver.org/) — with the caveat
in [README.md](README.md#status) that `0.x` is deliberate while the schema has
no migrations.

---

## [0.3.0] — 2026-08-30

Everything needed to put an instance on the public internet with a login you are
willing to print. Nothing here is demo-specific plumbing: the limits, the reset
path and the read-only tier are product.

### Added

- **A `demo` role** — a viewer with the writing taken away. It watches cameras
  and changes nothing: no recordings, no deletions, and **not its own password**,
  which is the point of the tier. A shared login the first visitor can change is
  a login you have given away, and one that can add a camera can point one at any
  address the server can reach. Created with
  `cam create-user you@example.com --role demo --team <slug>`.
- **Sign-in rate limiting**, on two windows. Per address stops a password list
  from one machine; per account stops the same list spread across many, which a
  per-address limit misses entirely. Only failures count and a success clears
  both, so mistyping twice and then getting it right is never rationed. The limit
  is checked *before* the password is verified — otherwise it is a slightly
  slower brute force that also spends the server's CPU on argon2.
- **`TRUSTED_PROXY_HOPS`**, because the above is worth nothing behind nginx
  without it: at the default of `0` every caller looks like the proxy and shares
  one bucket. A count rather than a boolean, since `X-Forwarded-For` is written
  by the client too and reading it blindly hands an attacker unlimited identities.
- **Password reset**, three ways in. An administrator issues a one-time link from
  the Admin page and hands it over — the path that works on a network with no
  outbound mail. Set `SMTP_HOST` and `SMTP_FROM` and the self-service form turns
  itself on; there is no second switch. `cam reset-link` and `cam set-password`
  work from the machine when nobody can get in at all.
- **Changing your own password**, at `/account`. The current one is required, so
  a session left open on a shared machine is not enough to take the account over.
- `cam create-user`, `cam set-password`, `cam reset-link`.
- **A landing page** in `site/`, static and self-contained: one HTML file with no
  build step, for Cloudflare Pages or anything else that serves a directory.

### Changed

- `/api/auth/me` reports `may_write`, so the dashboard drops the controls a
  demo account cannot use rather than rendering them and having every click come
  back 403.
- The minimum password length is `MIN_PASSWORD_LENGTH`, default 12, up from a
  hard-coded 10. A length floor and nothing else — composition rules push people
  towards `Password1!` and are worth less than four more characters.
- The dashboard says **Causeway**, not "Camera tunnel". A leftover from the
  rebrand, in the two places a person actually reads it.

### Notes on the reset token

It is stateless: signed with the app secret and carrying a keyed fingerprint of
the password hash it was issued against. That was a constraint rather than a
preference — a `password_resets` table needs a migration, and this schema has
none yet (ROADMAP entry 10), so a stored token would have been a feature that
could not be deployed to an instance already running. It pays for itself twice
anyway: redemption replaces the hash, so the link stops working without anything
marking it used, and any other password change invalidates every link
outstanding. Issuing a link changes nothing, so an administrator cannot lock
somebody out by pressing the button.

---

## [0.2.0] — 2026-08-30

First production deployment. Everything below was found or built while getting
it live, which is why so much of it is about failures explaining
themselves rather than about features.

### Added

- **Roles**: superadmin, admin and viewer, replacing the flat admin/member
  split. Viewers get Cameras and Recordings and nothing else — the other
  sections are absent rather than disabled. Roles are editable in place.
- **Hardware transcoding.** H.265 previews re-encode on NVENC/NVDEC when a card
  is visible inside the agent, and on libx264 otherwise, chosen by
  `TRANSCODE_ACCEL`. Frames stay on the card between decode and encode.
- **A capacity budget.** `GPU_BUDGET_PERCENT` / `CPU_BUDGET_PERCENT` of the
  machine, converted to a number of concurrent transcodes. Streams past it are
  refused with a message naming the limit, because a machine admitted past
  capacity does not fail — it stutters across every stream at once and reads
  like a network problem.
- **Camera IDs**, kept separate from names. Supported in the form, in pasted
  lists (`id, name, url`) and in CSV. A camera with no name given gets one built
  from its ID and address rather than being called after its IP.
- **Deleting** recordings (any team member, including viewers) and connection
  profiles (administrators). Both refuse with a reason when something depends on
  them.
- **Downloads are named** for the camera, the moment and the source, in UTC —
  `GATE-CAM-2026-08-29-162213Z-rtsp.mp4` rather than a folder of `session.mp4`.
- **A production runbook** with measured resource numbers, an nginx config, and
  a pre-handover checklist: [deploy/production.md](deploy/production.md).
- **Container images** for all three services, and a compose file that pulls
  rather than builds.
- `/api/health` now reports `version`, `ppp_available`, and the transcode
  accelerator and budget.

### Fixed

- **Credentials were double percent-encoded.** A password exported by an NVR as
  `CTC2.5%2B%2B` reached the camera as `CTC2.5%2B%2B` instead of `CTC2.5++`, and
  the camera answered 401 — a credentials error for credentials that were
  correct. Fixed in both places that read userinfo out of a URL.
- **A pasted space broke a VPN login.** A leading space in a username is
  invisible in a form field and comes back from the gateway as "check the
  username and password". Every pasted credential field is trimmed now.
- **Recordings could strand.** `upload_file` raises `S3UploadFailedError`, which
  is neither `ClientError` nor `BotoCoreError`, so a missing bucket escaped
  uncaught and left the recording in `finalizing` for ever with no reason on it.
- **A shared preview died when one viewer left.** Claims on a stream were keyed
  by user, so one browser's two mounts cancelled each other out: first frame,
  black, "connection dropped". Claims are per view now.
- **Two previews of one camera raced.** A stream is only registered once ready,
  so two starts inside that window both built one and then contended for the
  same tunnel.
- **`health()` reported a tunnel that was never dialled.** `tunl0` is a kernel
  placeholder present in every namespace and its name starts with `tun`, so a
  prefix match found it and everything downstream trusted it.
- **`ip netns add` needs more than `NET_ADMIN`.** Creating a namespace is a
  mount: `SYS_ADMIN` and an AppArmor exemption as well. All three are documented
  in the compose file with the error each one produces when missing.
- **A stale SSH control socket disabled multiplexing silently.** ssh will not
  bind a `ControlPath` that already exists — it connects anyway, so the dial
  succeeds and every later forward is refused.
- **The agent logged a full traceback every two seconds** while the database
  was unreachable. One line now, then a backoff, then a line when it recovers.
- **The `cam` CLI was missing from the images**, because `uv sync` runs before
  the source is copied and never installs the project.

### Changed

- `DATABASE_URL` is derived from `POSTGRES_*` rather than being a second copy of
  the password. Two copies of one secret is a trap, and it caught us.
- Preview startup went from about 20 seconds to about 5: MediaMTX no longer
  advertises unreachable ICE candidates, and ffmpeg no longer spends five
  seconds analysing a live stream it already knows the shape of.
- `PREVIEW_HOST` accepts a list, so a deployment reached from both the internet
  and its own LAN can name both.
- MediaMTX is pinned. `latest` moved a running deployment from 1.16 to 1.20 on
  an ordinary restart.
- Preview transcodes only what a browser cannot decode; everything else, and
  every recording, is a stream copy.

---

## [0.1.0] — 2026-08-28

Phase 1. The VPN drivers, the gate ladder, namespaces and the SSH tunnel
manager, the recorder with its redial ladder and gap accounting, storage and
retention against a hosted Versity gateway, the API and the dashboard.
