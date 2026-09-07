"use client";

import { useEffect, useState } from "react";
import { ApiError, api } from "@/lib/api";
import { ROLE_LABELS, type Me } from "@/lib/types";
import { Banner, Button, Card, CardHeader, Eyebrow, Field, PasswordInput } from "@/components/ui";

/**
 * Your own account.
 *
 * Only the password, because that is the only thing about an account its owner
 * changes: the role and the teams are somebody else's decision by design, and
 * putting them on this page would only be a way to ask for something the
 * server is going to refuse.
 */
export default function AccountPage() {
  const [me, setMe] = useState<Me | null>(null);
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [done, setDone] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.me().then(setMe).catch(() => {});
  }, []);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (next !== confirm) {
      setError("The two passwords do not match.");
      return;
    }
    setBusy(true);
    setError("");
    setDone(false);
    try {
      await api.changePassword(current, next);
      setDone(true);
      setCurrent("");
      setNext("");
      setConfirm("");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not change the password.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex max-w-xl flex-col gap-6">
      <div>
        <Eyebrow>Account</Eyebrow>
        <h1 className="mt-1 text-2xl font-semibold tracking-tight">
          {me?.display_name || me?.email || "Account"}
        </h1>
        {me ? (
          <p className="mt-1.5 text-sm text-fg-3">
            {me.email} · {ROLE_LABELS[me.role]}
            {me.teams.length ? ` · ${me.teams.map((t) => t.name).join(", ")}` : ""}
          </p>
        ) : null}
      </div>

      {me && !me.may_write ? (
        <Banner tone="warn" title="This is a demo account">
          It can watch cameras and change nothing, including its own password.
        </Banner>
      ) : (
        <Card className="p-6">
          <CardHeader
            title="Change your password"
            sub="Your current one is required, so a session left open on a shared machine is not enough to take the account over."
          />
          <form onSubmit={submit} className="mt-5 flex flex-col gap-4">
            <Field label="Current password">
              <PasswordInput
                autoComplete="current-password"
                value={current}
                onChange={(e) => setCurrent(e.target.value)}
                required
              />
            </Field>
            <Field
              label="New password"
              hint="At least twelve characters. Length beats punctuation."
            >
              <PasswordInput
                autoComplete="new-password"
                value={next}
                onChange={(e) => setNext(e.target.value)}
                required
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

            {error ? <Banner tone="bad" title={error} /> : null}
            {done ? (
              <Banner tone="ok" title="Password changed">
                Any reset link outstanding for this account has stopped working.
              </Banner>
            ) : null}

            <Button type="submit" variant="primary" disabled={busy} className="mt-1">
              {busy ? "Changing…" : "Change password"}
            </Button>
          </form>
        </Card>
      )}
    </div>
  );
}
