"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { ApiError, api } from "@/lib/api";
import { Button, Card, Field, Input } from "@/components/ui";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api.login(email, password);
      router.push("/");
      router.refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not sign in.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center px-4">
      <div className="w-full max-w-sm">
        <div className="mb-7">
          <p className="font-mono text-2xs uppercase tracking-[0.16em] text-steel">
            Camera tunnel
          </p>
          <h1 className="mt-1.5 text-2xl font-semibold tracking-tight">Control plane</h1>
        </div>

        <Card className="p-6">
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
            <Field label="Password">
              <Input
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
              />
            </Field>

            {error ? (
              <p className="rounded border border-bad/40 bg-bad-wash px-3 py-2 text-xs text-bad">
                {error}
              </p>
            ) : null}

            <Button type="submit" variant="primary" disabled={busy} className="mt-1">
              {busy ? "Signing in…" : "Sign in"}
            </Button>
          </form>
        </Card>
      </div>
    </main>
  );
}
