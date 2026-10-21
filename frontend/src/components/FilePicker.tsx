import { Paperclip } from "lucide-react";
import { useCallback, useRef, type ChangeEvent } from "react";

import { formatBytes } from "../lib/format";

export interface FilePickerProps {
  onPick: (files: File[]) => void;
  disabled?: boolean;
}

/** Hard limits from CONTRACTS_V2 §7 (`max_upload_bytes`) and the product spec. */
export const MAX_FILE_BYTES = 25 * 1024 * 1024;
export const MAX_FILES = 10;

const ACCEPT_ATTRIBUTE =
  "image/*,video/*,audio/*,.pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.txt,.csv,.zip,.rar,.7z";

/**
 * Paperclip button + hidden file input (CONTRACTS_V2 §6).
 *
 * Validation happens client-side so the operator gets an instant, specific
 * Russian explanation instead of an opaque 413 from the backend; the backend
 * still enforces the same limit authoritatively.
 */
export function FilePicker({ onPick, disabled = false }: FilePickerProps): JSX.Element {
  const inputRef = useRef<HTMLInputElement | null>(null);

  const openDialog = useCallback(() => {
    if (disabled) return;
    inputRef.current?.click();
  }, [disabled]);

  const handleChange = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      const input = event.currentTarget;
      const selected = Array.from(input.files ?? []);

      /* Always clear the native value: re-picking the same file must fire `change`
         again, and keeping the list alive would leak File handles between picks. */
      input.value = "";

      if (selected.length === 0) return;

      const problems: string[] = [];

      /* 1. Empty files are never uploadable — say so and drop them. */
      const nonEmpty = selected.filter((file) => {
        if (file.size === 0) {
          problems.push(`«${file.name}» — пустой файл (0 Б) и пропущен.`);
          return false;
        }
        return true;
      });

      /* 2. Per-file size ceiling (CONTRACTS_V2 §7: 25 МБ). */
      const tooLarge = nonEmpty.filter((file) => file.size > MAX_FILE_BYTES);
      const withinSize = nonEmpty.filter((file) => file.size <= MAX_FILE_BYTES);
      if (tooLarge.length > 0) {
        const list = tooLarge.map((file) => `«${file.name}» (${formatBytes(file.size)})`).join(", ");
        problems.push(
          `Файлы больше ${formatBytes(MAX_FILE_BYTES)} пропущены: ${list}. Максимальный размер одного файла — ${formatBytes(MAX_FILE_BYTES)}.`,
        );
      }

      /* 3. Count ceiling — reported once, against the files that survived 1 and 2. */
      const accepted = withinSize.slice(0, MAX_FILES);
      if (withinSize.length > MAX_FILES) {
        problems.push(
          withinSize.length === nonEmpty.length
            ? `Выбрано ${withinSize.length} файлов, можно прикрепить не более ${MAX_FILES}. Прикреплены первые ${MAX_FILES}.`
            : `Можно прикрепить не более ${MAX_FILES} файлов за раз — прикреплены первые ${MAX_FILES} допустимых.`,
        );
      }

      if (problems.length > 0) {
        window.alert(problems.join("\n\n"));
      }

      if (accepted.length > 0) onPick(accepted);
    },
    [onPick],
  );

  return (
    <div className="shrink-0">
      <button
        type="button"
        onClick={openDialog}
        disabled={disabled}
        className={`grid h-9 w-9 place-items-center rounded-full transition ${
          disabled
            ? "opacity-50 cursor-not-allowed text-tg-textFaint"
            : "text-tg-textMuted hover:bg-tg-hover hover:text-tg-accentHover"
        }`}
        title="Прикрепить файл"
        aria-label="Прикрепить файл"
        aria-disabled={disabled}
      >
        <Paperclip size={18} />
      </button>

      <input
        ref={inputRef}
        type="file"
        multiple
        accept={ACCEPT_ATTRIBUTE}
        onChange={handleChange}
        disabled={disabled}
        className="hidden"
        tabIndex={-1}
        aria-hidden="true"
      />
    </div>
  );
}
