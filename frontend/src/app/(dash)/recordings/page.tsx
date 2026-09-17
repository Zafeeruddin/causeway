"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { ApiError, api } from "@/lib/api";
import { ago, bytes, duration } from "@/lib/format";
import { useLive } from "@/lib/useLive";
import { writes, type DownloadLink, type LiveEvent, type Me, type Recording, type RecordingState } from "@/lib/types";
import { Pagination } from "@/components/Pagination";
import {
  Badge, Banner, Button, Card, Empty, Eyebrow, Input, Modal, type Tone,
} from "@/components/ui";

const PAGE_SIZE = 25;

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

/** States where the agent still owns the recording; the server refuses these too. */
const IN_FLIGHT: RecordingState[] = ["queued", "recording", "recovering", "finalizing"];
const STILL_RUNNING = new Set<RecordingState>(IN_FLIGHT);

/**
 * The filters worth a click.
 *
 * "Running" is four states rather than one, which is why the endpoint takes a
 * repeated `state` parameter: nobody wants "queued" on its own.
 */
const FILTERS: { label: string; states: RecordingState[] }[] = [
  { label: "All", states: [] },
  { label: "Running", states: IN_FLIGHT },
  { label: "Complete", states: ["complete"] },
  { label: "Failed", states: ["failed"] },
  { label: "Cancelled", states: ["cancelled"] },
];

export default function RecordingsPage() {
  const [recordings, setRecordings] = useState<Recording[]>([]);
  const [total, setTotal] = useState(0);
  const [pages, setPages] = useState(1);
  const [page, setPage] = useState(1);
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState("All");
  const [loading, setLoading] = useState(true);
  const [links, setLinks] = useState<DownloadLink[] | null>(null);
  const [confirming, setConfirming] = useState<Recording | null>(null);
  const [removing, setRemoving] = useState<string | null>(null);
  const [sending, setSending] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [me, setMe] = useState<Me | null>(null);
  const loadRequest = useRef(0);
  const onScreen = useRef<Set<string>>(new Set());

  const load = useCallback(async () => {
    const request = ++loadRequest.current;
    setLoading(true);
    try {
      const states = FILTERS.find((option) => option.label === filter)?.states ?? [];
      const result = await api.recordingPage(search, states, page, PAGE_SIZE);
      // A slow first request must not overwrite a fast second one.
      if (request !== loadRequest.current) return;
      setRecordings(result.items);
      setTotal(result.total);
      setPages(result.pages);
      // Deleting the last row of the last page leaves the person on a page that
      // no longer exists; the server says which page it actually served.
      if (result.page !== page) setPage(result.page);
    } finally {
      if (request === loadRequest.current) setLoading(false);
    }
  }, [filter, page, search]);

  useEffect(() => {
    load().catch((err) => {
      setError(err instanceof ApiError ? err.message : "The recordings could not be loaded.");
    });
  }, [load]);

  useEffect(() => {
    api.me().then(setMe).catch(() => {});
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setPage(1);
      setSearch(query.trim());
    }, 250);
    return () => window.clearTimeout(timer);
  }, [query]);

  useEffect(() => {
    onScreen.current = new Set(recordings.map((recording) => recording.id));
  }, [recordings]);

  // Deleting removes the files, and sending one again spends the storage
  // budget. The server refuses a demo account either way; dropping the buttons
  // keeps that from being a surprise.
  const mayWrite = me ? me.may_write && writes(me.role) : false;
  const unfiltered = filter === "All" && !search;

  useLive(
    useCallback(
      (event: LiveEvent) => {
        if (event.type !== "recording") return;
        const update = event.payload as unknown as Recording;
        if (onScreen.current.has(update.id)) {
          setRecordings((current) =>
            current.map((recording) =>
              recording.id === update.id
                ? // The agent publishes without the camera's name, so keep the
                  // one already on the row rather than blanking it.
                  {
                    ...recording,
                    ...update,
                    camera_name: update.camera_name || recording.camera_name,
                  }
                : recording,
            ),
          );
          return;
        }
        // A recording that is not on screen. Pulling it in would fight the page
        // or filter the person chose -- except on the unfiltered first page,
        // which is exactly where they land after pressing Record.
        if (page === 1 && unfiltered) void load();
      },
      [load, page, unfiltered],
    ),
  );

  function clearFilters() {
    setQuery("");
    setFilter("All");
    setPage(1);
  }

  async function remove(recording: Recording) {
    setRemoving(recording.id);
    setConfirming(null);
    try {
      await api.deleteRecording(recording.id);
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not delete the recording.");
    } finally {
      setRemoving(null);
    }
  }

  async function sendAgain(recording: Recording) {
    setSending(recording.id);
    setError("");
    try {
      await api.reshipRecording(recording.id);
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not send the recording again.");
    } finally {
      setSending(null);
    }
  }

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
        <h1 className="mt-1 text-2xl font-semibold tracking-tight">
          {total} recording{total === 1 ? "" : "s"}
          {unfiltered ? "" : " found"}
        </h1>
        <p className="mt-1.5 max-w-2xl text-sm text-fg-3">
          A recording is a set of sealed segments plus a record of when the stream was
          absent, so an outage costs the seconds it lasted rather than the whole session.
          Compare plays the raw camera feed and the inferred feed of the same moment side
          by side.
        </p>
      </div>

      {error ? <Banner tone="bad" title={error} /> : null}

      <div className="flex flex-col gap-2 lg:flex-row lg:items-center">
        <div className="flex flex-wrap gap-1">
          {FILTERS.map((option) => (
            <Button
              key={option.label}
              size="sm"
              variant={option.label === filter ? "primary" : "quiet"}
              aria-pressed={option.label === filter}
              onClick={() => {
                setPage(1);
                setFilter(option.label);
              }}
            >
              {option.label}
            </Button>
          ))}
        </div>
        <div className="min-w-0 flex-1">
          <Input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search by camera name, camera ID, or location…"
            aria-label="Search recordings"
            maxLength={200}
          />
        </div>
        <span className="shrink-0 font-mono text-2xs text-fg-3 tnum">
          {loading
            ? "Searching…"
            : total
              ? `${(page - 1) * PAGE_SIZE + 1}–${Math.min(page * PAGE_SIZE, total)} of ${total}`
              : "No results"}
        </span>
      </div>

      {confirming ? (
        <Modal
          open
          onClose={() => setConfirming(null)}
          title="Delete this recording?"
          sub="The video files go too, and nothing here can bring them back."
        >
          <div className="flex flex-col gap-4">
            <p className="text-sm text-fg-2">
              {confirming.camera_name || confirming.camera_id.slice(0, 8)} ·{" "}
              {confirming.total_bytes ? bytes(confirming.total_bytes) : "no files"} ·{" "}
              {ago(confirming.started_at ?? null)}
            </p>
            <div className="flex justify-end gap-2">
              <Button onClick={() => setConfirming(null)}>Keep it</Button>
              <Button variant="danger" onClick={() => remove(confirming)}>
                Delete permanently
              </Button>
            </div>
          </div>
        </Modal>
      ) : null}

      <Card>
        {recordings.length === 0 ? (
          <Empty
            title={unfiltered ? "Nothing recorded yet" : "No recordings match that"}
            hint={
              unfiltered
                ? "Select cameras on the Cameras page and press Record."
                : "Try another filter, or search for a different camera."
            }
            action={
              unfiltered ? undefined : <Button onClick={clearFilters}>Clear filters</Button>
            }
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
                    <td className="px-5 py-3 font-medium">
                      {recording.camera_name || recording.camera_id.slice(0, 8)}
                    </td>
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
                        {/* The capture worked and only the upload failed, so
                            the footage is still on the work volume. */}
                        {recording.can_reship && mayWrite ? (
                          <Button
                            size="sm"
                            variant="primary"
                            disabled={sending === recording.id}
                            onClick={() => sendAgain(recording)}
                          >
                            {sending === recording.id ? "Sending…" : "Retry upload"}
                          </Button>
                        ) : null}
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
                        {mayWrite ? (
                          <Button
                            size="sm"
                            variant="danger"
                            // In-flight recordings belong to the agent, which is
                            // still writing them; the server refuses those too.
                            disabled={STILL_RUNNING.has(recording.state) || removing === recording.id}
                            onClick={() => setConfirming(recording)}
                          >
                            {removing === recording.id ? "Deleting…" : "Delete"}
                          </Button>
                        ) : null}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Pagination page={page} pages={pages} setPage={setPage} label="Recording pages" />

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
