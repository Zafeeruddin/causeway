"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { ApiError, api } from "@/lib/api";
import { Button, Card, Field, PasswordInput } from "@/components/ui";

/**
 * Redeeming a reset link.
 *
 * Ends at the sign-in page rather than signing anybody in. A reset that logs
 * you straight in turns one readable email into a session, so a link forwarded
 * by accident would be the account handed over rather than a password to
 * change again.
 */
function ResetForm() {
  const router = useRouter();
  const token = useSearchParams().get("token") ?? "";
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (password !== confirm) {
      setError("The two passwords do not match.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      await api.resetPassword(token, password);
      setDone(true);
      setTimeout(() => router.replace("/login"), 2500);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not set the password.");
    } finally {
      setBusy(false);
    }
  }

  if (!token) {
    return (
      <Card className="p-6">
        <p className="text-sm text-fg-2">
          This page needs the link from your reset email. Open the link itself rather than
          this address.
        </p>
      </Card>
    );
  }

  if (done) {
    return (
      <Card className="p-6">
        <p className="text-sm text-fg-2">
          Password set. Signing in is the next step — taking you there now.
        </p>
      </Card>
    );
  }

  return (
    <Card className="p-6">
      <form onSubmit={submit} className="flex flex-col gap-4">
        <Field label="New password" hint="At least twelve characters. Length beats punctuation.">
          <PasswordInput
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
            autoFocus
          />
        </Field>
        <Field label="Again">
          <PasswordInput
            autoComplete="new-password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            required
          />
        </Field>

        {error ? (
          <p className="rounded border border-bad/40 bg-bad-wash px-3 py-2 text-xs text-bad">
            {error}
          </p>
        ) : null}

        <Button type="submit" variant="primary" disabled={busy} className="mt-1">
          {busy ? "Setting…" : "Set the password"}
        </Button>
      </form>
    </Card>
  );
}

export default function ResetPasswordPage() {
  return (
    <main className="flex min-h-screen items-center justify-center px-4">
      <div className="w-full max-w-sm">
        <div className="mb-7">
          <p className="font-mono text-2xs uppercase tracking-[0.16em] text-steel">Causeway</p>
          <h1 className="mt-1.5 text-2xl font-semibold tracking-tight">Choose a password</h1>
        </div>

        {/* useSearchParams needs a boundary or the whole route opts out of
            static rendering at build time. */}
        <Suspense fallback={<Card className="p-6"><span /></Card>}>
          <ResetForm />
        </Suspense>

        <p className="mt-4 text-center text-xs text-fg-3">
          <Link href="/login" className="transition hover:text-fg">
            Back to sign in
          </Link>
        </p>
      </div>
    </main>
  );
}
