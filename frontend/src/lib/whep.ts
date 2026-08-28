/**
 * WHEP (WebRTC-HTTP Egress Protocol) in one HTTP exchange: our offer goes up as
 * SDP, the server's answer comes back as SDP, and everything after that is
 * between the browser and MediaMTX.
 *
 * Written by hand rather than pulled from a package because the whole protocol
 * is the fifty lines below, and a preview player is not worth a dependency that
 * ships its own WebRTC opinions.
 */

export type WhepState = "connecting" | "live" | "lost" | "closed";

export interface WhepSession {
  /** Attach to a <video>; tracks land on it as they are negotiated. */
  readonly stream: MediaStream;
  /** Safe to call repeatedly, and safe to call before negotiation finished. */
  close(): void;
}

/** Anything that went wrong talking to the WHEP endpoint, with the server's words when it gave any. */
export class WhepError extends Error {}

/**
 * Some browsers never fire the final `icegatheringstatechange`, so waiting for
 * it unconditionally leaves the player on a spinner that will never resolve.
 * Whatever candidates exist by then are enough for a stream on the same network.
 */
const ICE_GATHERING_TIMEOUT_MS = 3000;

export async function openWhep(
  whepUrl: string,
  onState: (state: WhepState) => void,
): Promise<WhepSession> {
  const pc = new RTCPeerConnection();
  const stream = new MediaStream();
  const negotiation = new AbortController();

  /**
   * The resource URL from the answer's Location header. Deleting it releases
   * the server's half of the session immediately instead of leaving MediaMTX to
   * notice the silence.
   */
  let resource: string | null = null;
  let closed = false;

  const releaseResource = () => {
    const url = resource;
    resource = null;
    // keepalive so the request still leaves a tab that is being closed.
    if (url) void fetch(url, { method: "DELETE", keepalive: true }).catch(() => {});
  };

  const close = () => {
    if (closed) return;
    closed = true;
    negotiation.abort();
    for (const track of stream.getTracks()) track.stop();
    pc.close();
    releaseResource();
    onState("closed");
  };

  pc.ontrack = (event) => {
    if (!stream.getTracks().includes(event.track)) stream.addTrack(event.track);
  };

  pc.onconnectionstatechange = () => {
    if (closed) return;
    if (pc.connectionState === "connected") onState("live");
    // "disconnected" is reported as lost as well: the stream is gone from the
    // viewer's point of view either way, and MediaMTX drops the source rather
    // than waiting for an ICE restart that this player never performs.
    else if (pc.connectionState === "failed" || pc.connectionState === "disconnected") {
      onState("lost");
    }
  };

  onState("connecting");

  // Receive-only in both directions: this player never sends media, and
  // declaring the transceivers up front is what puts m-lines in the offer.
  pc.addTransceiver("video", { direction: "recvonly" });
  pc.addTransceiver("audio", { direction: "recvonly" });

  await pc.setLocalDescription(await pc.createOffer());
  await waitForIceGathering(pc);

  let response: Response;
  try {
    response = await fetch(whepUrl, {
      method: "POST",
      headers: { "content-type": "application/sdp" },
      body: pc.localDescription?.sdp ?? "",
      signal: negotiation.signal,
    });
  } catch (cause) {
    pc.close();
    throw new WhepError(
      closed
        ? "The preview was closed before it finished connecting."
        : "The preview stream could not be reached from this browser.",
      { cause },
    );
  }

  if (!response.ok) {
    pc.close();
    const body = (await response.text().catch(() => "")).trim();
    throw new WhepError(body || `The preview stream answered ${response.status}.`);
  }

  const answer = await response.text();
  resource = absolute(response.headers.get("location"), whepUrl);

  if (closed) {
    // Torn down mid-negotiation. The server still opened a session for the
    // offer it just answered, so hand it back rather than waiting for a timeout.
    releaseResource();
    throw new WhepError("The preview was closed before it finished connecting.");
  }

  await pc.setRemoteDescription({ type: "answer", sdp: answer });
  return { stream, close };
}

/**
 * WHEP has one request and one response, so there is nowhere to send candidates
 * gathered after the offer is sent: the offer has to carry all of them.
 */
function waitForIceGathering(pc: RTCPeerConnection): Promise<void> {
  if (pc.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    const finish = () => {
      clearTimeout(timer);
      pc.removeEventListener("icegatheringstatechange", check);
      resolve();
    };
    const check = () => {
      if (pc.iceGatheringState === "complete") finish();
    };
    const timer = setTimeout(finish, ICE_GATHERING_TIMEOUT_MS);
    pc.addEventListener("icegatheringstatechange", check);
  });
}

/** Servers commonly answer with a path-only Location, which fetch cannot delete. */
function absolute(location: string | null, base: string): string | null {
  if (!location) return null;
  try {
    return new URL(location, base).toString();
  } catch {
    return null;
  }
}
