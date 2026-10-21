/**
 * Отдельная форма входа администратора — CONTRACTS_V2.md §3.3 / §6.
 *
 * Полностью независима от деск-логина (`components/LoginScreen.tsx`):
 * ходит в `/api/admin/login`, хранит свой токен (`tgdesk.admin_token`) и после
 * успеха уводит на `/admin/panel`. Любой оператор без роли ADMIN получит от
 * сервера 403 «Требуются права администратора» — текст выводим как есть.
 */

import { AlertCircle, ArrowLeft, Eye, EyeOff, Loader2, Lock, LogIn, ShieldCheck, User } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";

import { useAdminStore } from "../store/useAdminStore";

const PANEL_PATH = "/admin/panel";
const DESK_PATH = "/";

export default function AdminLoginPage(): JSX.Element {
  const login = useAdminStore((state) => state.login);
  const loading = useAdminStore((state) => state.loginLoading);
  const error = useAdminStore((state) => state.loginError);
  const token = useAdminStore((state) => state.token);
  const clearError = useAdminStore((state) => state.clearError);

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [passwordVisible, setPasswordVisible] = useState(false);

  const usernameRef = useRef<HTMLInputElement>(null);
  const passwordRef = useRef<HTMLInputElement>(null);

  /* Автофокус на логине при открытии страницы. */
  useEffect(() => {
    usernameRef.current?.focus();
  }, []);

  /* Если админ уже вошёл в этой вкладке — сразу в панель. */
  useEffect(() => {
    if (token) window.location.href = PANEL_PATH;
  }, [token]);

  const onSubmit = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    if (loading) return;
    const success = await login(username, password);
    if (success) window.location.href = PANEL_PATH;
  };

  const togglePassword = (): void => {
    setPasswordVisible((previous) => !previous);
    /* Возвращаем фокус в поле, чтобы Enter и дальше отправлял форму. */
    window.setTimeout(() => passwordRef.current?.focus(), 0);
  };

  return (
    <div className="chat-wallpaper flex h-full w-full flex-col items-center justify-center overflow-y-auto p-6">
      <div className="w-full max-w-sm animate-slide-up">
        <div className="mb-6 flex flex-col items-center gap-3 text-center">
          <div className="grid h-16 w-16 place-items-center rounded-2xl bg-tg-accent/20 ring-1 ring-tg-accent/40">
            <ShieldCheck size={28} className="text-tg-accentHover" />
          </div>
          <div>
            <h1 className="text-xl font-semibold tracking-tight text-tg-text">Административная панель</h1>
            <p className="mt-1 text-xs text-tg-textMuted">TG-Desk CRM</p>
          </div>
        </div>

        <form
          onSubmit={(event) => void onSubmit(event)}
          noValidate
          className="space-y-3 rounded-2xl border border-tg-border bg-tg-panel p-5 shadow-pop"
        >
          <label className="block">
            <span className="mb-1 block text-2xs font-semibold uppercase tracking-wider text-tg-textFaint">Логин</span>
            <div className="flex items-center gap-2 rounded-lg border border-tg-border bg-tg-elevated px-2.5 focus-within:border-tg-accent/60">
              <User size={14} className="shrink-0 text-tg-textFaint" />
              <input
                ref={usernameRef}
                value={username}
                onChange={(event) => {
                  setUsername(event.target.value);
                  if (error) clearError();
                }}
                autoComplete="username"
                autoCapitalize="none"
                autoCorrect="off"
                spellCheck={false}
                disabled={loading}
                className="w-full bg-transparent py-2 text-xs text-tg-text placeholder:text-tg-textFaint focus:outline-none disabled:opacity-60"
                placeholder="admin"
              />
            </div>
          </label>

          <label className="block">
            <span className="mb-1 block text-2xs font-semibold uppercase tracking-wider text-tg-textFaint">Пароль</span>
            <div className="flex items-center gap-2 rounded-lg border border-tg-border bg-tg-elevated px-2.5 focus-within:border-tg-accent/60">
              <Lock size={14} className="shrink-0 text-tg-textFaint" />
              <input
                ref={passwordRef}
                type={passwordVisible ? "text" : "password"}
                value={password}
                onChange={(event) => {
                  setPassword(event.target.value);
                  if (error) clearError();
                }}
                autoComplete="current-password"
                disabled={loading}
                className="w-full bg-transparent py-2 text-xs text-tg-text placeholder:text-tg-textFaint focus:outline-none disabled:opacity-60"
                placeholder="••••••••"
              />
              <button
                type="button"
                onClick={togglePassword}
                tabIndex={-1}
                aria-label={passwordVisible ? "Скрыть пароль" : "Показать пароль"}
                title={passwordVisible ? "Скрыть пароль" : "Показать пароль"}
                className="grid h-6 w-6 shrink-0 place-items-center rounded text-tg-textFaint transition hover:bg-tg-hover hover:text-tg-text"
              >
                {passwordVisible ? <EyeOff size={14} /> : <Eye size={14} />}
              </button>
            </div>
          </label>

          {error && (
            <p
              role="alert"
              className="flex items-start gap-1.5 rounded-lg border border-tg-danger/40 bg-tg-danger/10 px-2.5 py-1.5 text-2xs text-tg-danger"
            >
              <AlertCircle size={12} className="mt-[1px] shrink-0" />
              <span>{error}</span>
            </p>
          )}

          <button
            type="submit"
            disabled={loading}
            className="flex w-full items-center justify-center gap-2 rounded-lg bg-tg-accent py-2.5 text-xs font-semibold text-white transition hover:bg-tg-accentHover disabled:cursor-not-allowed disabled:opacity-60"
          >
            {loading ? <Loader2 size={14} className="animate-spin" /> : <LogIn size={14} />}
            {loading ? "Проверка…" : "Войти"}
          </button>

          <div className="border-t border-tg-border pt-3">
            <button
              type="button"
              onClick={() => {
                window.location.href = DESK_PATH;
              }}
              className="flex items-center gap-1.5 text-2xs text-tg-textMuted transition hover:text-tg-text"
            >
              <ArrowLeft size={12} />
              Вернуться к работе
            </button>
          </div>
        </form>

        <p className="mt-4 text-center text-[10px] leading-relaxed text-tg-textFaint">
          Вход доступен только учётным записям с ролью ADMIN. Действия администраторов фиксируются в журнале.
        </p>
      </div>
    </div>
  );
}
