"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { LiveEvent } from "./types";

type Handler = (event: LiveEvent) => void;

/**
 * One WebSocket per tab, shared by every component that needs it.
 *
 * Reconnects with backoff, because the connection this dashboard is watching is
 * itself unreliable and a dropped socket must not look like a dropped tunnel.
 */
export function useLive(onEvent: Handler): { connected: boolean } {
  const [connected, setConnected] = useState(false);
  const handler = useRef(onEvent);
  handler.current = onEvent;

  useEffect(() => {
    let socket: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;
    let closed = false;

    const open = () => {
      if (closed) return;
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      socket = new WebSocket(`${protocol}//${window.location.host}/api/ws`);

      socket.onopen = () => {
        attempt = 0;
        setConnected(true);
      };
      socket.onmessage = (message) => {
        try {
          const event = JSON.parse(message.data) as LiveEvent;
          if (event.type !== "ping") handler.current(event);
        } catch {
          /* a malformed frame is not worth tearing the socket down for */
        }
      };
      socket.onclose = () => {
        setConnected(false);
        if (closed) return;
        // 1s, 2s, 4s ... capped at 15s, matching the recorder's redial ladder.
        const delay = Math.min(1000 * 2 ** attempt++, 15000);
        retry = setTimeout(open, delay);
      };
      socket.onerror = () => socket?.close();
    };

    open();
    return () => {
      closed = true;
      if (retry) clearTimeout(retry);
      socket?.close();
    };
  }, []);

  return { connected };
}

/** Convenience for pages that only care about one event type. */
export function useLiveOf(type: LiveEvent["type"], onEvent: Handler) {
  const filter = useCallback(
    (event: LiveEvent) => {
      if (event.type === type) onEvent(event);
    },
    [type, onEvent],
  );
  return useLive(filter);
}
