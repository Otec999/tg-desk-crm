import { AlertCircle, ArrowRight, Loader2, Lock, MessageSquare, ShieldCheck, User } from "lucide-react";
import { useState, type FormEvent } from "react";

import { useCRMStore } from "../store/useCRMStore";

const DEMO_ACCOUNTS = [
  { username: "op1", password: "operator123", role: "Operator 1" },
  { username: "op4", password: "operator123", role: "Operator 4" },
  { username: "admin", password: "admin123", role: "Supervisor" },
];

export function LoginScreen(): JSX.Element {
  const login = useCRMStore((state) => state.login);
  const authLoading = useCRMStore((state) => state.authLoading);
  const authError = useCRMStore((state) => state.authError);
  const mode = useCRMStore((state) => state.mode);

  const [username, setUsername] = useState("op1");
  const [password, setPassword] = useState("operator123");

  const onSubmit = async (event: FormEvent): Promise<void> => {
    event.preventDefault();
    await login(username.trim(), password);
  };

  return (
    <div className="chat-wallpaper flex h-full w-full items-center justify-center p-6">
      <div className="w-full max-w-sm animate-slide-up">
        <div className="mb-6 flex flex-col items-center gap-3 text-center">
          <div className="grid h-16 w-16 place-items-center rounded-2xl bg-tg-accent/20 ring-1 ring-tg-accent/40">
            <MessageSquare size={28} className="text-tg-accentHover" />
          </div>
          <div>
            <h1 className="text-xl font-semibold tracking-tight text-tg-text">TG-Desk CRM</h1>
            <p className="mt-1 text-xs text-tg-textMuted">
              Internal B2B Telegram operations desk — unified corporate account
            </p>
          </div>
        </div>

        <form
          onSubmit={(event) => void onSubmit(event)}
          className="space-y-3 rounded-2xl border border-tg-border bg-tg-panel p-5 shadow-pop"
        >
          <label className="block">
            <span className="mb-1 block text-2xs font-semibold uppercase tracking-wider text-tg-textFaint">
              Operator username
            </span>
            <div className="flex items-center gap-2 rounded-lg border border-tg-border bg-tg-elevated px-2.5 focus-within:border-tg-accent/60">
              <User size={14} className="text-tg-textFaint" />
              <input
                value={username}
                onChange={(event) => setUsername(event.target.value)}
                autoComplete="username"
                required
                className="w-full bg-transparent py-2 text-xs text-tg-text focus:outline-none"
                placeholder="op1"
              />
            </div>
          </label>

          <label className="block">
            <span className="mb-1 block text-2xs font-semibold uppercase tracking-wider text-tg-textFaint">Password</span>
            <div className="flex items-center gap-2 rounded-lg border border-tg-border bg-tg-elevated px-2.5 focus-within:border-tg-accent/60">
              <Lock size={14} className="text-tg-textFaint" />
              <input
                type="password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                autoComplete="current-password"
                required
                className="w-full bg-transparent py-2 text-xs text-tg-text focus:outline-none"
                placeholder="••••••••"
              />
            </div>
          </label>

          {authError && (
            <p className="flex items-start gap-1.5 rounded-lg border border-tg-danger/40 bg-tg-danger/10 px-2.5 py-1.5 text-2xs text-tg-danger">
              <AlertCircle size={12} className="mt-[1px] shrink-0" />
              <span>{authError}</span>
            </p>
          )}

          <button
            type="submit"
            disabled={authLoading}
            className="flex w-full items-center justify-center gap-2 rounded-lg bg-tg-accent py-2.5 text-xs font-semibold text-white transition hover:bg-tg-accentHover disabled:opacity-60"
          >
            {authLoading ? <Loader2 size={14} className="animate-spin" /> : <ArrowRight size={14} />}
            {authLoading ? "Signing in…" : mode === "mock" ? "Open demo workspace" : "Sign in"}
          </button>

          <div className="border-t border-tg-border pt-3">
            <p className="mb-1.5 flex items-center gap-1 text-[10px] uppercase tracking-wider text-tg-textFaint">
              <ShieldCheck size={10} /> Seeded accounts
            </p>
            <div className="grid grid-cols-3 gap-1.5">
              {DEMO_ACCOUNTS.map((account) => (
                <button
                  key={account.username}
                  type="button"
                  onClick={() => {
                    setUsername(account.username);
                    setPassword(account.password);
                  }}
                  className="rounded-md border border-tg-border bg-tg-elevated px-1.5 py-1 text-[10px] text-tg-textMuted transition hover:border-tg-accent/50 hover:text-tg-text"
                >
                  <span className="block font-semibold text-tg-text">{account.username}</span>
                  <span>{account.role}</span>
                </button>
              ))}
            </div>
          </div>
        </form>

        <p className="mt-4 text-center text-[10px] text-tg-textFaint">
          If the API is unreachable the desk falls back to the bundled demo dataset automatically.
        </p>
      </div>
    </div>
  );
}
