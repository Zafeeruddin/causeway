"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { ApiError, api } from "@/lib/api";
import { ago, bytes, duration } from "@/lib/format";
import { useLive } from "@/lib/useLive";
import type { Camera, DownloadLink, LiveEvent, Recording, RecordingState } from "@/lib/types";
import {
  Badge, Banner, Button, Card, CardHeader, Empty, Eyebrow, Modal, type Tone,
} from "@/components/ui";

const TONE: Record<RecordingState, Tone> = {
  queued: "muted",
  recording: "steel",
  recovering: "warn",
  finalizing: "steel",
  complete: "ok",
  failed: "bad",
  cancelled: "muted",
};

const LABEL: Record<RecordingState, string> = {
  queued: "queued",
  recording: "recording",
  recovering: "recovering",
  finalizing: "finalizing",
  complete: "complete",
  failed: "failed",
  cancelled: "cancelled",
};

export default function RecordingsPage() {
  const [recordings, setRecordings] = useState<Recording[]>([]);
  const [cameras, setCameras] = useState<Camera[]>([]);
  const [links, setLinks] = useState<DownloadLink[] | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setRecordings(await api.recordings());
  }, []);

  useEffect(() => {
    load().catch(() => {});
    api.cameras().then(setCameras).catch(() => {});
  }, [load]);

  useLive(
    useCallback((event: LiveEvent) => {
      if (event.type !== "recording") return;
      const update = event.payload as unknown as Recording;
      setRecordings((current) =>
        current.some((r) => r.id === update.id)
          ? current.map((r) => (r.id === update.id ? { ...r, ...update } : r))
          : [update, ...current],
      );
    }, []),
  );

  const nameFor = (id: string) => cameras.find((c) => c.id === id)?.name ?? id.slice(0, 8);

  async function openDownloads(recording: Recording) {
    setError("");
    try {
      setLinks(await api.downloads(recording.id));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not build download links.");
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <div>
        <Eyebrow>Recordings</Eyebrow>
        <h1 className="mt-1 text-2xl font-semibold tracking-tight">Recordings</h1>
        <p className="mt-1.5 max-w-2xl text-sm text-fg-3">
          A recording is a set of sealed segments plus a record of when the stream was
          absent, so an outage costs the seconds it lasted rather than the whole session.
          Compare plays the raw camera feed and the inferred feed of the same moment side
          by side.
        </p>
      </div>

      {error ? <Banner tone="bad" title={error} /> : null}

      <Card>
        <CardHeader title="All recordings" sub={`${recordings.length} total`} />
        {recordings.length === 0 ? (
          <Empty
            title="Nothing recorded yet"
            hint="Select cameras on the Cameras page and press Record."
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[820px] text-sm">
              <thead>
                <tr className="border-b border-line text-left">
                  {["camera", "state", "asked for", "captured", "gaps", "size", "when", ""].map(
                    (heading) => (
                      <th
                        key={heading}
                        className="whitespace-nowrap px-5 py-2.5 font-mono text-2xs font-medium uppercase tracking-[0.13em] text-fg-3"
                      >
                        {heading}
                      </th>
                    ),
                  )}
                </tr>
              </thead>
              <tbody>
                {recordings.map((recording) => (
                  <tr key={recording.id} className="border-b border-line-soft last:border-0">
                    <td className="px-5 py-3 font-medium">{nameFor(recording.camera_id)}</td>
                    <td className="px-5 py-3">
                      <Badge tone={TONE[recording.state]}>{LABEL[recording.state]}</Badge>
                    </td>
                    <td className="px-5 py-3 font-mono text-xs text-fg-2 tnum">
                      {duration(recording.requested_seconds)}
                    </td>
                    <td className="px-5 py-3 font-mono text-xs text-fg-2 tnum">
                      {recording.captured_seconds ? duration(recording.captured_seconds) : "—"}
                    </td>
                    <td className="px-5 py-3 font-mono text-xs tnum">
                      {recording.gap_seconds ? (
                        <span className="text-warn">{duration(recording.gap_seconds)}</span>
                      ) : (
                        <span className="text-fg-3">none</span>
                      )}
                    </td>
                    <td className="px-5 py-3 font-mono text-xs text-fg-2 tnum">
                      {recording.total_bytes ? bytes(recording.total_bytes) : "—"}
                    </td>
                    <td className="px-5 py-3 text-xs text-fg-3">
                      {ago(recording.started_at ?? null)}
                    </td>
                    <td className="px-5 py-3">
                      <div className="flex items-center justify-end gap-2">
                        {recording.state === "complete" ? (
                          <Link href={`/recordings/${recording.id}`}>
                            <Button size="sm" variant="primary">
                              Compare
                            </Button>
                          </Link>
                        ) : (
                          <Button size="sm" disabled>
                            Compare
                          </Button>
                        )}
                        <Button
                          size="sm"
                          disabled={recording.state !== "complete"}
                          onClick={() => openDownloads(recording)}
                        >
                          Download
                        </Button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Modal
        open={links !== null}
        onClose={() => setLinks(null)}
        title="Download"
        sub="Links are signed and expire in an hour."
      >
        <ul className="flex flex-col gap-2">
          {(links ?? []).map((link) => (
            <li
              key={link.filename}
              className="flex items-center gap-3 rounded border border-line bg-ink px-4 py-3"
            >
              <Badge tone={link.source_kind === "rtsp" ? "steel" : link.source_kind ? "zone" : "muted"}>
                {link.source_kind ?? "session"}
              </Badge>
              <span className="min-w-0 flex-1 truncate font-mono text-xs text-fg-2">
                {link.filename}
              </span>
              <span className="font-mono text-2xs text-fg-3 tnum">{bytes(link.bytes)}</span>
              <a
                href={link.url}
                className="rounded bg-steel px-3 py-1.5 text-xs font-medium text-ink hover:bg-steel/85"
              >
                Save
              </a>
            </li>
          ))}
        </ul>
      </Modal>
    </div>
  );
}
