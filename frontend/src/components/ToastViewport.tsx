import { AlertTriangle, CheckCircle2, Info, X, XCircle, type LucideIcon } from "lucide-react";

import type { ToastKind } from "../store/useCRMStore";
import { useCRMStore } from "../store/useCRMStore";

const ICONS: Record<ToastKind, LucideIcon> = {
  info: Info,
  success: CheckCircle2,
  warning: AlertTriangle,
  error: XCircle,
};

const TONES: Record<ToastKind, string> = {
  info: "border-tg-info/40 bg-tg-info/10 text-tg-info",
  success: "border-tg-success/40 bg-tg-success/10 text-tg-success",
  warning: "border-tg-warning/50 bg-tg-warning/10 text-tg-warning",
  error: "border-tg-danger/50 bg-tg-danger/10 text-tg-danger",
};

export function ToastViewport(): JSX.Element {
  const toasts = useCRMStore((state) => state.toasts);
  const dismissToast = useCRMStore((state) => state.dismissToast);

  return (
    <div className="pointer-events-none fixed bottom-5 left-1/2 z-50 flex w-[min(92vw,460px)] -translate-x-1/2 flex-col gap-2">
      {toasts.map((toast) => {
        const Icon = ICONS[toast.kind];
        return (
          <div
            key={toast.id}
            role="status"
            className={`pointer-events-auto flex animate-slide-up items-start gap-2.5 rounded-lg border px-3 py-2.5 text-xs shadow-pop backdrop-blur ${TONES[toast.kind]}`}
          >
            <Icon size={15} className="mt-0.5 shrink-0" />
            <span className="flex-1 text-tg-text">{toast.message}</span>
            <button
              type="button"
              onClick={() => dismissToast(toast.id)}
              className="shrink-0 rounded p-0.5 text-tg-textFaint transition hover:text-tg-text"
              aria-label="Dismiss notification"
            >
              <X size={13} />
            </button>
          </div>
        );
      })}
    </div>
  );
}
