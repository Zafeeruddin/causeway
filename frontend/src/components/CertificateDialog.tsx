"use client";

import { useState } from "react";
import { fingerprint as group } from "@/lib/format";
import { Banner, Button, Modal } from "./ui";

/**
 * The certificate question, asked once per profile.
 *
 * This is the same decision the FortiClient desktop dialog puts in front of
 * people, so it is presented the same way: the host, the reason, and the
 * fingerprint laid out to be compared by eye.
 *
 * One thing worth the extra line of copy: the desktop client shows a SHA-1
 * fingerprint while the headless client pins SHA-256, so the two never match
 * and nobody should be told to compare them.
 */
export function CertificateDialog({
  open,
  onClose,
  onAccept,
  host,
  fingerprint,
  algorithm = "sha256",
  reason,
  busy,
}: {
  open: boolean;
  onClose: () => void;
  onAccept: (fingerprint: string) => void;
  host: string;
  fingerprint: string;
  algorithm?: string;
  reason?: string;
  busy?: boolean;
}) {
  const [confirmed, setConfirmed] = useState(false);

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Trust this gateway?"
      sub="Asked once. The answer is pinned to this profile."
    >
      <div className="flex flex-col gap-4">
        <Banner tone="warn" title={`${host} presented a certificate we have not seen before.`}>
          {reason ? <span className="font-mono">{reason}</span> : null}
        </Banner>

        <div className="rounded border border-line bg-ink px-4 py-3">
          <p className="font-mono text-2xs uppercase tracking-[0.14em] text-fg-3">
            {algorithm} fingerprint
          </p>
          <p className="mt-2 break-all font-mono text-xs leading-relaxed text-fg">
            {group(fingerprint)}
          </p>
        </div>

        <p className="text-xs leading-relaxed text-fg-3">
          Check this against the fingerprint your network team published for{" "}
          <span className="font-mono text-fg-2">{host}</span>. Note that the FortiClient
          desktop window shows a <span className="font-mono">SHA-1</span> fingerprint, which
          will not match this one &mdash; they are different algorithms, not different
          certificates.
        </p>

        <label className="flex items-start gap-2.5 text-xs text-fg-2">
          <input
            type="checkbox"
            checked={confirmed}
            onChange={(e) => setConfirmed(e.target.checked)}
            className="mt-0.5 h-3.5 w-3.5 accent-[#79b7d8]"
          />
          <span>I have checked this fingerprint against a trusted source.</span>
        </label>

        <div className="flex justify-end gap-2 pt-1">
          <Button onClick={onClose} disabled={busy}>
            Deny
          </Button>
          <Button
            variant="primary"
            disabled={!confirmed || busy}
            onClick={() => onAccept(fingerprint)}
          >
            {busy ? "Connecting…" : "Accept and connect"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
