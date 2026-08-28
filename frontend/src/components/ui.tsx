"use client";

import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode, SelectHTMLAttributes } from "react";

export function cx(...parts: (string | false | null | undefined)[]) {
  return parts.filter(Boolean).join(" ");
}

/* ---- surfaces ---- */

export function Card({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div className={cx("rounded border border-line bg-ink-2", className)}>{children}</div>
  );
}

export function CardHeader({ title, sub, action }: { title: ReactNode; sub?: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-4 border-b border-line-soft px-5 py-4">
      <div className="min-w-0">
        <h2 className="truncate text-sm font-semibold text-fg">{title}</h2>
        {sub ? <p className="mt-0.5 text-xs text-fg-3">{sub}</p> : null}
      </div>
      {action}
    </div>
  );
}

export function Eyebrow({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <span className={cx("font-mono text-2xs uppercase tracking-[0.14em] text-fg-3", className)}>
      {children}
    </span>
  );
}

export function Empty({ title, hint, action }: { title: string; hint?: string; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center gap-3 px-6 py-14 text-center">
      <p className="text-sm text-fg-2">{title}</p>
      {hint ? <p className="max-w-md text-xs text-fg-3">{hint}</p> : null}
      {action}
    </div>
  );
}

/* ---- controls ---- */

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "ghost" | "danger" | "quiet";
  size?: "sm" | "md";
};

export function Button({ variant = "ghost", size = "md", className, ...rest }: ButtonProps) {
  const base =
    "inline-flex items-center justify-center gap-2 rounded font-medium transition " +
    "disabled:cursor-not-allowed disabled:opacity-40";
  const sizes = { sm: "px-2.5 py-1 text-xs", md: "px-3.5 py-2 text-sm" };
  const variants = {
    primary: "bg-steel text-ink hover:bg-steel/85",
    ghost: "border border-line bg-ink-3 text-fg-2 hover:border-steel/60 hover:text-fg",
    quiet: "text-fg-3 hover:text-fg",
    danger: "border border-bad/40 text-bad hover:bg-bad/10",
  };
  return <button className={cx(base, sizes[size], variants[variant], className)} {...rest} />;
}

export function Field({
  label, hint, error, children,
}: { label: string; hint?: string; error?: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="text-xs font-medium text-fg-2">{label}</span>
      {children}
      {error ? (
        <span className="text-xs text-bad">{error}</span>
      ) : hint ? (
        <span className="text-xs text-fg-3">{hint}</span>
      ) : null}
    </label>
  );
}

const controlClass =
  "w-full rounded border border-line bg-ink px-3 py-2 text-sm text-fg placeholder:text-fg-3/70 " +
  "focus:border-steel focus:outline-none";

export function Input({ className, ...rest }: InputHTMLAttributes<HTMLInputElement>) {
  return <input className={cx(controlClass, className)} {...rest} />;
}

export function Select({ className, children, ...rest }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select className={cx(controlClass, "appearance-none", className)} {...rest}>
      {children}
    </select>
  );
}

export function Textarea({
  className, ...rest
}: React.TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea className={cx(controlClass, "font-mono text-xs leading-relaxed", className)} {...rest} />;
}

/* ---- state ---- */

export type Tone = "ok" | "warn" | "bad" | "steel" | "zone" | "muted";

const TONES: Record<Tone, string> = {
  ok: "border-ok/40 bg-ok-wash text-ok",
  warn: "border-warn/40 bg-warn-wash text-warn",
  bad: "border-bad/40 bg-bad-wash text-bad",
  steel: "border-steel/40 bg-steel-wash text-steel",
  zone: "border-zone/40 bg-zone-wash text-zone",
  muted: "border-line bg-ink-3 text-fg-3",
};

export function Badge({ tone = "muted", children }: { tone?: Tone; children: ReactNode }) {
  return (
    <span
      className={cx(
        "inline-flex items-center gap-1.5 rounded border px-2 py-0.5 font-mono text-2xs uppercase tracking-wide",
        TONES[tone],
      )}
    >
      {children}
    </span>
  );
}

/** A filled dot. Shape carries the state as well as colour, for scanning. */
export function Dot({ tone = "muted" }: { tone?: Tone }) {
  const fills: Record<Tone, string> = {
    ok: "bg-ok", warn: "bg-warn", bad: "bg-bad",
    steel: "bg-steel", zone: "bg-zone", muted: "bg-fg-3",
  };
  return <span className={cx("inline-block h-1.5 w-1.5 shrink-0 rounded-full", fills[tone])} />;
}

export function Banner({ tone, title, children }: { tone: Tone; title: string; children?: ReactNode }) {
  return (
    <div className={cx("rounded border px-4 py-3", TONES[tone])}>
      <p className="text-sm font-medium">{title}</p>
      {children ? <div className="mt-1 text-xs opacity-90">{children}</div> : null}
    </div>
  );
}

export function Spinner({ className }: { className?: string }) {
  return (
    <span
      aria-hidden
      className={cx(
        "inline-block h-3 w-3 animate-spin rounded-full border border-current border-t-transparent",
        className,
      )}
    />
  );
}

/* ---- overlay ---- */

export function Modal({
  open, onClose, title, sub, children, width = "max-w-lg",
}: {
  open: boolean; onClose: () => void; title: string; sub?: string;
  children: ReactNode; width?: string;
}) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/70 p-4 pt-16">
      <div
        className={cx("w-full rounded border border-line bg-ink-2 shadow-2xl", width)}
        role="dialog"
        aria-modal="true"
      >
        <div className="flex items-start justify-between gap-4 border-b border-line-soft px-5 py-4">
          <div>
            <h2 className="text-sm font-semibold">{title}</h2>
            {sub ? <p className="mt-0.5 text-xs text-fg-3">{sub}</p> : null}
          </div>
          <Button variant="quiet" size="sm" onClick={onClose} aria-label="Close">
            ✕
          </Button>
        </div>
        <div className="px-5 py-5">{children}</div>
      </div>
    </div>
  );
}
