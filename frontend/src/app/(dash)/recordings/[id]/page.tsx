"use client";

/**
 * The side-by-side view: the raw camera feed and the inferred feed of the same
 * camera, at the same moment.
 *
 * The transport runs on wall-clock time, not on either file's own clock. A
 * session file is a concatenation of what that feed captured, so a fifteen
 * second outage is not fifteen seconds of black -- it is simply absent, and
 * everything after it sits fifteen seconds earlier in the file than it happened
 * in the world. Driving both players from one media position would quietly
 * compare frames minutes apart, which looks exactly like the inference being
 * wrong. So each panel converts the shared moment into its own position, and a
 * feed that missed the moment says so instead of showing something else.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { ApiError, api } from "@/lib/api";
import { ago, bytes, duration } from "@/lib/format";
import { CAUSE_TONE, KIND_LABEL, clockAt, gapAt, mediaTimeAt, timecode } from "@/lib/playback";
import type { Comparison, Track } from "@/lib/types";
import { Badge, Banner, Button, Card, Empty, Eyebrow, Spinner, cx } from "@/components/ui";

/** Seek a player rather than let it drift further than this. Small enough that
 *  nobody sees it, large enough that ordinary decode jitter is not a seek. */
const CORRECTION_SECONDS = 0.35;

/** The readouts do not need sixty updates a second; the videos play on their
 *  own clocks and this only has to keep the scrubber honest. */
const PUBLISH_INTERVAL_MS = 66;

const RATES = [0.25, 0.5, 1, 2];

export default function ComparePage() {
  const { id } = useParams<{ id: string }>();
  const [data, setData] = useState<Comparison | null>(null);
  const [error, setError] = useState("");
  const [at, setAt] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [rate, setRate] = useState(1);
  const [trim, setTrim] = useState(0);
  const [audible, setAudible] = useState<string | null>(null);
  const videos = useRef<Record<string, HTMLVideoElement | null>>({});

  const load = useCallback(async () => {
    setError("");
    try {
      setData(await api.comparison(id));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load this recording.");
    }
  }, [id]);

  useEffect(() => {
    load().catch(() => {});
  }, [load]);

  const window_ = data?.window_seconds ?? 0;

  /* ---- the shared clock ---- */

  useEffect(() => {
    if (!playing || !data) return;
    let frame = 0;
    let last = performance.now();
    let published = last;
    let position = at;

    const tick = (now: number) => {
      position += ((now - last) / 1000) * rate;
      last = now;
      if (position >= data.window_seconds) {
        setAt(data.window_seconds);
        setPlaying(false);
        return;
      }
      if (now - published >= PUBLISH_INTERVAL_MS) {
        published = now;
        setAt(position);
      }
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
    // `at` is the starting position for this run of the clock, deliberately not
    // a dependency: it changes sixty times a second while this effect runs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playing, rate, data]);

  /* ---- the players, slaved to it ---- */

  useEffect(() => {
    if (!data) return;
    for (const track of data.tracks) {
      const element = videos.current[track.source_kind];
      if (!element) continue;

      // Only the inferred feed is trimmed: the delay being taken out is the
      // inference pipeline's, and it does not apply to the camera.
      const moment = at + (track.source_kind === "hls" ? trim : 0);
      const target = mediaTimeAt(track.spans, moment);

      if (target === null) {
        if (!element.paused) element.pause();
        continue;
      }
      if (Math.abs(element.currentTime - target) > CORRECTION_SECONDS) {
        element.currentTime = target;
      }
      element.playbackRate = rate;
      element.muted = audible !== track.source_kind;
      if (playing && element.paused) void element.play().catch(() => setPlaying(false));
      if (!playing && !element.paused) element.pause();
    }
  }, [at, playing, rate, trim, audible, data]);

  /* ---- keys ---- */

  const nudge = useCallback(
    (by: number) => setAt((current) => Math.min(Math.max(current + by, 0), window_)),
    [window_],
  );

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target && ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName)) return;
      if (event.code === "Space") {
        event.preventDefault();
        setPlaying((p) => !p);
      } else if (event.key === "ArrowLeft") {
        nudge(event.shiftKey ? -5 : -1);
      } else if (event.key === "ArrowRight") {
        nudge(event.shiftKey ? 5 : 1);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [nudge]);

  if (error && !data) {
    return (
      <Card>
        <Empty
          title={error}
          hint="A recording can only be compared once it has finished and been uploaded."
          action={
            <Link href="/recordings">
              <Button size="sm">Back to recordings</Button>
            </Link>
          }
        />
      </Card>
    );
  }
  if (!data) {
    return (
      <div className="flex items-center gap-3 px-1 py-16 text-sm text-fg-3">
        <Spinner /> Loading both feeds…
      </div>
    );
  }

  const lonely = data.tracks.length < 2;

  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <Eyebrow>Compare</Eyebrow>
          <h1 className="mt-1 text-2xl font-semibold tracking-tight">{data.camera_name}</h1>
          <p className="mt-1.5 text-sm text-fg-3">
            {duration(data.requested_seconds)} asked for · started {ago(data.origin)} ·{" "}
            {new Date(data.origin).toLocaleString(undefined, { hour12: false })}
          </p>
        </div>
        <Link href="/recordings">
          <Button size="sm">Back to recordings</Button>
        </Link>
      </div>

      {error ? <Banner tone="bad" title={error} /> : null}
      {lonely ? (
        <Banner tone="warn" title="Only one feed was recorded">
          There is nothing to compare this against. Add an HLS source to the camera and record
          again to see the inferred feed beside the raw one.
        </Banner>
      ) : null}

      <div className={cx("grid gap-4", lonely ? "" : "xl:grid-cols-2")}>
        {data.tracks.map((track) => (
          <Panel
            key={track.source_kind}
            track={track}
            at={at + (track.source_kind === "hls" ? trim : 0)}
            audible={audible === track.source_kind}
            onAudio={() =>
              setAudible((current) =>
                current === track.source_kind ? null : track.source_kind,
              )
            }
            onError={() =>
              setError("These links have expired. Reload the page for fresh ones.")
            }
            attach={(element) => {
              videos.current[track.source_kind] = element;
            }}
          />
        ))}
      </div>

      <Card className="p-4">
        <Transport
          at={at}
          window={window_}
          origin={data.origin}
          playing={playing}
          rate={rate}
          tracks={data.tracks}
          onToggle={() => setPlaying((p) => !p)}
          onSeek={(value) => setAt(value)}
          onNudge={nudge}
          onRate={setRate}
        />

        <div className="mt-4 flex flex-wrap items-center gap-x-6 gap-y-3 border-t border-line-soft pt-4">
          <div>
            <Eyebrow>Inferred feed trim</Eyebrow>
            <div className="mt-1.5 flex items-center gap-2">
              <Button size="sm" onClick={() => setTrim((t) => Math.round((t - 0.1) * 10) / 10)}>
                −0.1s
              </Button>
              <span className="w-16 text-center font-mono text-xs text-fg-2 tnum">
                {trim > 0 ? "+" : ""}
                {trim.toFixed(1)}s
              </span>
              <Button size="sm" onClick={() => setTrim((t) => Math.round((t + 0.1) * 10) / 10)}>
                +0.1s
              </Button>
              {trim !== 0 ? (
                <Button size="sm" variant="quiet" onClick={() => setTrim(0)}>
                  reset
                </Button>
              ) : null}
            </div>
          </div>
          <p className="max-w-xl flex-1 text-xs leading-relaxed text-fg-3">
            {data.alignment.note}
          </p>
        </div>
      </Card>
    </div>
  );
}

/* ---- one feed ---- */

function Panel({
  track,
  at,
  audible,
  onAudio,
  onError,
  attach,
}: {
  track: Track;
  at: number;
  audible: boolean;
  onAudio: () => void;
  onError: () => void;
  attach: (element: HTMLVideoElement | null) => void;
}) {
  const missing = mediaTimeAt(track.spans, at) === null;
  const gap = missing ? gapAt(track.gaps, at) : null;

  return (
    <Card>
      <div className="flex items-center justify-between gap-3 border-b border-line-soft px-4 py-2.5">
        <div className="flex items-center gap-2.5">
          <Badge tone={track.source_kind === "rtsp" ? "steel" : "zone"}>
            {track.source_kind}
          </Badge>
          <span className="text-sm font-medium">{KIND_LABEL[track.source_kind]}</span>
        </div>
        <div className="flex items-center gap-3 font-mono text-2xs text-fg-3 tnum">
          <span>{duration(track.captured_seconds)} captured</span>
          {track.gap_seconds ? (
            <span className="text-warn">{duration(track.gap_seconds)} missing</span>
          ) : null}
          <span>{bytes(track.bytes)}</span>
          <Button size="sm" variant={audible ? "primary" : "quiet"} onClick={onAudio}>
            {audible ? "audio on" : "audio off"}
          </Button>
        </div>
      </div>

      <div className="relative aspect-video bg-black">
        <video
          ref={attach}
          src={track.url}
          className="h-full w-full object-contain"
          preload="auto"
          playsInline
          muted
          onError={onError}
        />
        {missing ? (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-ink/85 px-6 text-center">
            <p className="text-sm text-fg-2">Nothing was recorded at this moment.</p>
            {gap ? (
              <p className="max-w-sm text-xs text-fg-3">
                <span className="font-mono uppercase tracking-wider">{gap.cause}</span>
                {gap.detail ? ` — ${gap.detail}` : ""} · {duration(gap.seconds)}
              </p>
            ) : (
              <p className="text-xs text-fg-3">This feed started later than the other one.</p>
            )}
          </div>
        ) : null}
      </div>
    </Card>
  );
}

/* ---- the shared transport ---- */

function Transport({
  at,
  window: total,
  origin,
  playing,
  rate,
  tracks,
  onToggle,
  onSeek,
  onNudge,
  onRate,
}: {
  at: number;
  window: number;
  origin: string;
  playing: boolean;
  rate: number;
  tracks: Track[];
  onToggle: () => void;
  onSeek: (value: number) => void;
  onNudge: (by: number) => void;
  onRate: (value: number) => void;
}) {
  const percent = (seconds: number) => `${total ? (seconds / total) * 100 : 0}%`;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-2">
        <Button size="sm" variant="quiet" onClick={() => onNudge(-5)} title="Shift + ←">
          −5s
        </Button>
        <Button size="sm" variant="quiet" onClick={() => onNudge(-1)} title="←">
          −1s
        </Button>
        <Button variant="primary" onClick={onToggle} className="w-20" title="Space">
          {playing ? "Pause" : "Play"}
        </Button>
        <Button size="sm" variant="quiet" onClick={() => onNudge(1)} title="→">
          +1s
        </Button>
        <Button size="sm" variant="quiet" onClick={() => onNudge(5)} title="Shift + →">
          +5s
        </Button>

        <div className="ml-2 font-mono text-sm text-fg tnum">{timecode(at)}</div>
        <div className="font-mono text-2xs text-fg-3 tnum">
          / {timecode(total)} · {clockAt(origin, at)}
        </div>

        <div className="ml-auto flex items-center gap-1">
          {RATES.map((value) => (
            <Button
              key={value}
              size="sm"
              variant={value === rate ? "primary" : "quiet"}
              onClick={() => onRate(value)}
            >
              {value}×
            </Button>
          ))}
        </div>
      </div>

      {/* One row per feed, so an outage on one side is visible as a stretch of
          the timeline where only the other side has anything. */}
      <div className="flex flex-col gap-1">
        {tracks.map((track) => (
          <div key={track.source_kind} className="flex items-center gap-2">
            <span className="w-10 font-mono text-2xs uppercase text-fg-3">
              {track.source_kind}
            </span>
            <div className="relative h-1.5 flex-1 overflow-hidden rounded bg-ok/25">
              {track.starts_at > 0 ? (
                <span
                  className="absolute inset-y-0 left-0 bg-line"
                  style={{ width: percent(track.starts_at) }}
                />
              ) : null}
              {track.gaps.map((gap, index) => (
                <span
                  key={index}
                  className={cx("absolute inset-y-0", CAUSE_TONE[gap.cause] ?? "bg-fg-3")}
                  style={{ left: percent(gap.wall_start), width: percent(gap.seconds) }}
                  title={`${gap.cause}: ${gap.detail}`}
                />
              ))}
              <span
                className="absolute inset-y-0 w-px bg-fg"
                style={{ left: percent(at) }}
              />
            </div>
          </div>
        ))}
      </div>

      <input
        type="range"
        min={0}
        max={total || 1}
        step={0.05}
        value={at}
        onChange={(event) => onSeek(Number(event.target.value))}
        className="w-full accent-steel"
        aria-label="Position"
      />
    </div>
  );
}
