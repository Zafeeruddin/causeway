export type HlsState = "connecting" | "live" | "lost" | "closed";

export interface HlsSession {
  close(): void;
}

/**
 * Play the MediaMTX HLS rendition. Safari uses its native player; other modern
 * browsers use hls.js. Both travel over the page's existing HTTPS connection,
 * so this path works when a carrier, CGNAT or corporate firewall blocks ICE.
 */
export async function openHls(
  video: HTMLVideoElement,
  url: string,
  onState: (state: HlsState) => void,
): Promise<HlsSession> {
  let closed = false;
  let reportedLive = false;
  let networkRecoveries = 0;
  let mediaRecoveries = 0;
  let hls: import("hls.js").default | null = null;

  const live = () => {
    if (closed || reportedLive) return;
    reportedLive = true;
    onState("live");
  };
  const lost = () => {
    if (!closed) onState("lost");
  };
  const play = () => void video.play().catch(() => {});

  onState("connecting");
  video.addEventListener("playing", live);
  video.addEventListener("loadeddata", live);
  video.addEventListener("error", lost);

  if (video.canPlayType("application/vnd.apple.mpegurl")) {
    video.src = url;
    play();
  } else {
    // Most viewers stay on WebRTC and Safari has native HLS, so only download
    // hls.js on browsers that actually need the compatibility path.
    const { default: Hls, ErrorTypes, Events } = await import("hls.js");
    if (closed) return closedSession();
    if (!Hls.isSupported()) {
      queueMicrotask(lost);
      return session();
    }
    // Tuned for the network this path exists to serve. Everyone whose ICE
    // works stays on WebRTC and never gets here, so the viewers who do are the
    // ones on CGNAT, a corporate egress, or a phone -- high latency, and the
    // last people who should be pinned to the live edge.
    //
    // At liveSyncDurationCount: 1 the player starts on the newest segment in a
    // seven-segment window. A round trip long enough for one more segment to
    // roll means asking for one MediaMTX has already evicted, which is a 404
    // and a fatal network error. Starting three back costs about two seconds of
    // latency and puts a whole window between the player and the eviction edge.
    hls = new Hls({
      lowLatencyMode: true,
      backBufferLength: 30,
      liveSyncDurationCount: 3,
      liveMaxLatencyDurationCount: 10,
      maxLiveSyncPlaybackRate: 1.5,
    });
    hls.on(Events.MEDIA_ATTACHED, () => hls?.loadSource(url));
    hls.on(Events.MANIFEST_PARSED, play);
    hls.on(Events.ERROR, (_event, data) => {
      if (!data.fatal || closed || !hls) return;
      // One retry is not enough on a live edge. A segment expiring under a slow
      // client is transient by nature -- the next playlist has newer ones -- and
      // giving up after a single 404 is what made a preview fail on the first
      // open and play on the second, when the stream had been running long
      // enough to have a settled window.
      if (data.type === ErrorTypes.NETWORK_ERROR && networkRecoveries++ < 4) {
        hls.startLoad();
        return;
      }
      if (data.type === ErrorTypes.MEDIA_ERROR && mediaRecoveries++ < 2) {
        hls.recoverMediaError();
        return;
      }
      lost();
    });
    hls.attachMedia(video);
  }

  return session();

  function session(): HlsSession {
    return {
      close() {
        if (closed) return;
        closed = true;
        hls?.destroy();
        hls = null;
        video.removeEventListener("playing", live);
        video.removeEventListener("loadeddata", live);
        video.removeEventListener("error", lost);
        video.pause();
        video.removeAttribute("src");
        video.load();
        onState("closed");
      },
    };
  }

  function closedSession(): HlsSession {
    return { close() {} };
  }
}

/** Keep the random MediaMTX path intact while escaping each path component. */
export function hlsPreviewUrl(path: string): string {
  const escaped = path.split("/").map(encodeURIComponent).join("/");
  return `/hls/${escaped}/index.m3u8`;
}
