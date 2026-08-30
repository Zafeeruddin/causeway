# Changelog

Notable changes, newest first. Dates are when the change reached a running
deployment, not when it was written.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the versions follow [semantic versioning](https://semver.org/) — with the caveat
in [README.md](README.md#status) that `0.x` is deliberate while the schema has
no migrations.

---

## [0.2.0] — 2026-08-30

First production deployment. Everything below was found or built while getting
one customer live, which is why so much of it is about failures explaining
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
