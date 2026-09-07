"use client";

import Link from "next/link";
import { useState } from "react";
import { ApiError, api } from "@/lib/api";
import { Button, Card, Field, Input } from "@/components/ui";

/**
 * Asking for a reset link.
 *
 * The confirmation is deliberately the same sentence whatever address is
 * typed, because the server gives the same answer either way: a page that says
 * "no account with that address" is a page that lists your users to anyone
 * willing to type.
 */
export default function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api.forgotPassword(email);
      setSent(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not send the request.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center px-4">
      <div className="w-full max-w-sm">
        <div className="mb-7">
          <p className="font-mono text-2xs uppercase tracking-[0.16em] text-steel">Causeway</p>
          <h1 className="mt-1.5 text-2xl font-semibold tracking-tight">Reset your password</h1>
        </div>

        <Card className="p-6">
          {sent ? (
            <div className="flex flex-col gap-3">
              <p className="text-sm text-fg-2">
                If that address has an account, a reset link is on its way. It works once and
                stops working after an hour.
              </p>
              <p className="text-xs text-fg-3">
                Nothing arrived? This deployment may have no outbound mail configured, in
                which case an administrator can hand you a link directly.
              </p>
            </div>
          ) : (
            <form onSubmit={submit} className="flex flex-col gap-4">
              <Field label="Email">
                <Input
                  type="email"
                  autoComplete="username"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  required
                  autoFocus
                />
              </Field>

              {error ? (
                <p className="rounded border border-bad/40 bg-bad-wash px-3 py-2 text-xs text-bad">
                  {error}
                </p>
              ) : null}

              <Button type="submit" variant="primary" disabled={busy} className="mt-1">
                {busy ? "Sending…" : "Send a reset link"}
              </Button>
            </form>
          )}
        </Card>

        <p className="mt-4 text-center text-xs text-fg-3">
          <Link href="/login" className="transition hover:text-fg">
            Back to sign in
          </Link>
        </p>
      </div>
    </main>
  );
}
