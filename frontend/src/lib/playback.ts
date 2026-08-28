/**
 * Mirrors app/services/playback.py.
 *
 * The transport runs on wall-clock time and each feed converts it to its own
 * position in its own file. That indirection is the feature: the session file
 * is a concatenation of what was captured, so after an outage the same moment
 * sits at different offsets in the two files, and seeking both to the same
 * number compares the wrong frames.
 */

import type { GapMark, Span, Track } from "./types";

/** Where to seek this track for this moment, or null if it was not captured. */
export function mediaTimeAt(spans: Span[], at: number): number | null {
  for (const span of spans) {
    if (at >= span.wall_start && at < span.wall_start + span.seconds) {
      return span.media_start + (at - span.wall_start);
    }
  }
  return null;
}

/** The inverse: what moment a position in the file corresponds to. */
export function wallTimeAt(spans: Span[], media: number): number | null {
  for (const span of spans) {
    if (media >= span.media_start && media < span.media_start + span.seconds) {
      return span.wall_start + (media - span.media_start);
    }
  }
  return null;
}

/** The gap covering this moment, for the overlay that explains an empty panel. */
export function gapAt(gaps: GapMark[], at: number): GapMark | null {
  return gaps.find((g) => at >= g.wall_start && at < g.wall_start + g.seconds) ?? null;
}

export function coversEverything(track: Track, window: number): boolean {
  const only = track.spans.length === 1 ? track.spans[0] : undefined;
  return only !== undefined && only.seconds >= window - 0.5;
}

/** mm:ss.t — tenths, because the trim control moves in tenths. */
export function timecode(seconds: number): string {
  const clamped = Math.max(seconds, 0);
  const m = Math.floor(clamped / 60);
  const s = clamped - m * 60;
  return `${m}:${s.toFixed(1).padStart(4, "0")}`;
}

/** The wall-clock time of a moment, for the "when did this happen" readout. */
export function clockAt(origin: string, at: number): string {
  const date = new Date(new Date(origin).getTime() + at * 1000);
  return date.toLocaleTimeString(undefined, { hour12: false });
}

export const CAUSE_TONE: Record<string, string> = {
  vpn: "bg-bad",
  ssh: "bg-warn",
  camera: "bg-zone",
  unknown: "bg-fg-3",
};

export const KIND_LABEL: Record<string, string> = {
  rtsp: "Raw camera",
  hls: "Inferred feed",
};
