"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api } from "@/lib/api";
import { hlsPreviewUrl, openHls, type HlsSession } from "@/lib/hls";
import type { Camera, Preview, SourceKind } from "@/lib/types";
import { WhepError, openWhep, type WhepSession } from "@/lib/whep";
import { Badge, Button, Spinner } from "./ui";

/**
 * A live view of one camera.
 *
 * A preview holds a camera session open and cameras cap those hard, so teardown
 * is the load-bearing part of this component: every path that stops watching --
 * unmount, the Stop button, expiry, a start that finished after the component
 * was already gone -- has to reach the DELETE. Everything below is arranged
 * around that rather than around the happy path.
 */

type View =
  | { phase: "starting"; fallback?: boolean }
  | { phase: "live"; transport: "direct" | "compatible" }
  /** The start was refused. The message is the server's, quoted. */
  | { phase: "failed"; message: string }
  | { phase: "ended"; message: string };

/**
 * The mutable half of one watching attempt, kept in a ref rather than state.
 *
 * StrictMode mounts, unmounts and remounts in development, and the first
 * mount's `api.startPreview` typically resolves *after* its own cleanup ran.
 * Without somewhere outside React's render cycle to record "this attempt has
 * been abandoned", that response starts a stream nothing will ever stop.
 */
interface Attempt {
  abandoned: boolean;
  fallbackStarted: boolean;
  detachMediaListeners: (() => void) | null;
  preview: Preview | null;
  session: WhepSession | HlsSession | null;
}

export function LivePreview({
  camera,
  sourceKind,
}: {
  camera: Pick<Camera, "id" | "name">;
  sourceKind?: SourceKind;
}) {
  const [view, setView] = useState<View>({ phase: "starting" });
  const [preview, setPreview] = useState<Preview | null>(null);
  const [restarts, setRestarts] = useState(0);
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const attemptRef = useRef<Attempt | null>(null);

  const teardown = useCallback((attempt: Attempt | null) => {
    if (!attempt || attempt.abandoned) return;
    attempt.abandoned = true;
    attempt.detachMediaListeners?.();
    attempt.detachMediaListeners = null;
    attempt.session?.close();
    attempt.session = null;
    const started = attempt.preview;
    attempt.preview = null;
    if (started) {
      void api.stopPreview(started.camera_id, started.id, started.viewer).catch(() => {});
    }
  }, []);

  /** An ending is not a failure, and must not overwrite the server's refusal. */
  const end = useCallback((message: string) => {
    setView((current) => (current.phase === "failed" ? current : { phase: "ended", message }));
  }, []);

  useEffect(() => {
    const attempt: Attempt = {
      abandoned: false,
      fallbackStarted: false,
      detachMediaListeners: null,
      preview: null,
      session: null,
    };
    attemptRef.current = attempt;
    setView({ phase: "starting" });
    setPreview(null);

    void (async () => {
      try {
        const started = await api.startPreview(camera.id, sourceKind);
        if (attempt.abandoned) {
          // Cleanup ran while the request was in flight, so it had nothing to
          // stop. Hand the stream back here instead of leaking it.
          void api.stopPreview(started.camera_id, started.id, started.viewer).catch(() => {});
          return;
        }
        attempt.preview = started;
        setPreview(started);

        let directEverLive = false;

        const useCompatiblePreview = (failure?: unknown) => {
          if (attempt.abandoned || attempt.fallbackStarted) return;
          attempt.fallbackStarted = true;
          attempt.detachMediaListeners?.();
          attempt.detachMediaListeners = null;
          attempt.session?.close();
          attempt.session = null;

          const video = videoRef.current;
          if (!video) return;
          video.srcObject = null;
          setView({ phase: "starting", fallback: true });

          let fallbackEverLive = false;
          void openHls(video, hlsPreviewUrl(started.path), (state) => {
              if (attempt.abandoned) return;
              if (state === "live") {
                fallbackEverLive = true;
                setView({ phase: "live", transport: "compatible" });
              } else if (state === "lost") {
                setView({
                  phase: "failed",
                  message: fallbackEverLive
                    ? "The connection to the stream dropped."
                    : compatiblePreviewFailed(failure, started.codec),
                });
              }
            })
            .then((fallbackSession) => {
              if (attempt.abandoned) fallbackSession.close();
              else attempt.session = fallbackSession;
            })
            .catch((error) => {
              if (!attempt.abandoned) {
                setView({
                  phase: "failed",
                  message: compatiblePreviewFailed(error, started.codec),
                });
              }
            });

          window.setTimeout(() => {
            if (attempt.abandoned || fallbackEverLive) return;
            setView({
              phase: "failed",
              message: compatiblePreviewFailed(failure, started.codec),
            });
          }, FALLBACK_MEDIA_TIMEOUT_MS);
        };

        // If WebRTC cannot negotiate this codec, Safari's native HLS support
        // can still play common camera HEVC streams. Go straight to that path
        // instead of knowingly waiting on a black WebRTC session.
        const undecodable = codecComplaint(started.codec);
        if (undecodable) {
          useCompatiblePreview(new Error(undecodable));
          return;
        }

        // Negotiation succeeding and video arriving are two different things,
        // and the gap between them is where every firewall shows up: the offer
        // and answer travel over HTTPS through the proxy, while the video comes
        // straight from the preview server over UDP. A deployment that has not
        // opened that port looks perfectly healthy right up to here.
        const directVideo = videoRef.current;
        const markDirectVideoLive = () => {
          if (attempt.abandoned || attempt.fallbackStarted) return;
          directEverLive = true;
          setView({ phase: "live", transport: "direct" });
        };
        directVideo?.addEventListener("playing", markDirectVideoLive);
        directVideo?.addEventListener("loadeddata", markDirectVideoLive);
        attempt.detachMediaListeners = () => {
          directVideo?.removeEventListener("playing", markDirectVideoLive);
          directVideo?.removeEventListener("loadeddata", markDirectVideoLive);
        };

        let session: WhepSession;
        try {
          session = await openWhep(started.whep_url, (state) => {
            if (attempt.abandoned) return;
            if (state === "lost") {
              useCompatiblePreview();
            }
          });
        } catch (error) {
          useCompatiblePreview(error);
          return;
        }
        if (attempt.abandoned) {
          session.close();
          return;
        }
        // A failed connection can emit its state while setRemoteDescription is
        // still resolving. In that race the fallback already owns the video;
        // do not overwrite its session with the dead peer connection.
        if (attempt.fallbackStarted) {
          session.close();
          return;
        }
        attempt.session = session;
        if (videoRef.current) videoRef.current.srcObject = session.stream;

        // ICE can also simply never resolve, in which case no state arrives at
        // all and the player would sit on "Connecting…" for ever.
        window.setTimeout(() => {
          if (attempt.abandoned || directEverLive || attempt.fallbackStarted) return;
          useCompatiblePreview();
        }, MEDIA_TIMEOUT_MS);
      } catch (error) {
        if (attempt.abandoned) return;
        setView({ phase: "failed", message: explain(error) });
      }
    })();

    return () => {
      teardown(attempt);
      if (videoRef.current) videoRef.current.srcObject = null;
    };
  }, [camera.id, sourceKind, restarts, teardown, end]);

  // Past expires_at the server hard-stops the stream and the picture simply
  // freezes, which reads as a stalled dashboard rather than as a finished
  // preview. Say so, on the same clock the server is using.
  useEffect(() => {
    if (!preview) return;
    const remaining = new Date(preview.expires_at).getTime() - Date.now();
    const timer = setTimeout(() => {
      teardown(attemptRef.current);
      end("This preview reached its one-hour limit.");
    }, Math.max(remaining, 0));
    return () => clearTimeout(timer);
  }, [preview, teardown, end]);

  // The expiry hint is derived from the clock, so it needs a reason to re-render.
  const [, retick] = useState(0);
  useEffect(() => {
    if (view.phase !== "live") return;
    const timer = setInterval(() => retick((n) => n + 1), 30_000);
    return () => clearInterval(timer);
  }, [view.phase]);

  function stop() {
    teardown(attemptRef.current);
    end("You stopped this preview.");
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="relative aspect-video overflow-hidden rounded border border-line bg-black">
        {/* Kept mounted through every phase so the stream has somewhere to
            attach the moment negotiation finishes. */}
        <video
          ref={videoRef}
          autoPlay
          playsInline
          muted
          className="h-full w-full object-contain"
        />

        {view.phase === "starting" ? (
          <Overlay>
            <Spinner className="text-steel" />
            <p className="text-xs text-fg-2">Connecting to the camera…</p>
            <p className="max-w-xs text-2xs text-fg-3">
              {view.fallback
                ? "The direct video path is blocked. Switching to a compatible HTTPS preview…"
                : "The camera is dialled on demand, so the first frame can take a few seconds."}
            </p>
          </Overlay>
        ) : null}

        {view.phase === "failed" ? (
          <Overlay>
            <Badge tone="bad">preview refused</Badge>
            <p className="max-w-sm text-xs leading-relaxed text-fg-2">{view.message}</p>
            <Button size="sm" onClick={() => setRestarts((n) => n + 1)}>
              Retry
            </Button>
          </Overlay>
        ) : null}

        {view.phase === "ended" ? (
          <Overlay>
            <p className="text-xs text-fg-2">This preview ended</p>
            <p className="max-w-sm text-2xs text-fg-3">{view.message}</p>
            <Button size="sm" onClick={() => setRestarts((n) => n + 1)}>
              Restart
            </Button>
          </Overlay>
        ) : null}

        {view.phase === "live" ? (
          <div className="absolute left-3 top-3">
            <Badge tone="ok">
              {view.transport === "compatible" ? "live · compatible" : "live"}
            </Badge>
          </div>
        ) : null}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        {preview ? (
          <Badge tone={preview.source_kind === "rtsp" ? "steel" : "zone"}>
            {preview.source_kind}
          </Badge>
        ) : null}
        <span className="truncate text-xs text-fg-3">{camera.name}</span>
        {preview && view.phase === "live" ? (
          <span className="font-mono text-2xs text-fg-3 tnum">
            {untilExpiry(preview.expires_at)}
          </span>
        ) : null}
        <div className="ml-auto">
          {view.phase === "starting" || view.phase === "live" ? (
            <Button size="sm" variant="quiet" onClick={stop}>
              Stop
            </Button>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function Overlay({ children }: { children: React.ReactNode }) {
  return (
    <div className="absolute inset-0 flex flex-col items-center justify-center gap-2.5 bg-black/75 px-6 text-center">
      {children}
    </div>
  );
}

/**
 * The server writes 409 and 503 details for the person reading them -- "the
 * camera refused these credentials", "every preview slot is taken" -- and a
 * generic "could not start the preview" would throw away the only sentence that
 * tells anyone what to do next.
 */
function explain(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof WhepError) return error.message;
  return "The preview could not be started.";
}

/**
 * Whether this browser can decode what the camera is sending, and what to say
 * when it cannot.
 *
 * The preview is a stream copy on purpose -- transcoding fifteen cameras to
 * suit one browser is not free -- so the codec reaching the browser is the
 * camera's own. H.265 is the common one to trip on: cameras ship it by default
 * and only Safari and some hardware-accelerated Chrome builds decode it in
 * WebRTC. Recording is unaffected, which is worth saying, because "preview is
 * broken" and "this camera is broken" look identical from here.
 */
/** How long to wait for the first frame before calling the media leg blocked. */
const MEDIA_TIMEOUT_MS = 7_000;
const FALLBACK_MEDIA_TIMEOUT_MS = 15_000;

/**
 * The message for a stream that negotiated and then never arrived.
 *
 * By the time this is shown both the direct media path and the HTTPS fallback
 * have failed, so report the useful codec/negotiation reason when one exists.
 */
function compatiblePreviewFailed(directFailure: unknown, codec: string): string {
  const codecReason = directFailure instanceof Error ? directFailure.message : codecComplaint(codec);
  if (codecReason) return `${codecReason} The compatible HTTPS preview could not play it either.`;
  return (
    "Neither the direct low-latency connection nor the compatible HTTPS preview " +
    "could receive video from this camera."
  );
}

function codecComplaint(codec: string): string {
  if (!codec) return "";
  const supported = RTCRtpReceiver.getCapabilities?.("video")?.codecs ?? [];
  if (!supported.length) return ""; // Nothing to go on; let the negotiation decide.
  const wanted = `video/${codec}`.toLowerCase();
  if (supported.some((entry) => entry.mimeType.toLowerCase() === wanted)) return "";
  return (
    `This camera streams ${codec}, which this browser cannot decode. ` +
    "Recording it is unaffected - only watching it live is."
  );
}

function untilExpiry(expiresAt: string): string {
  const seconds = Math.max((new Date(expiresAt).getTime() - Date.now()) / 1000, 0);
  return seconds >= 60 ? `${Math.floor(seconds / 60)}m left` : `${Math.floor(seconds)}s left`;
}
