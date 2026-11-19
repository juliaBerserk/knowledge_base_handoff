import { useRef, useState } from "react";

type Mark = {
  start: number; end: number; label: string; contact_id: string | null;
  base_start: number | null; base_end: number | null;
};
type Range = { start: number; end: number };
type Choices = { work: string[]; hide: Range[] };
type Preview = { text: string; proof: string; markers: Mark[]; warning: string };
type Source = { format: string; content: Blob };

const EMPTY: Choices = { work: [], hide: [] };
const FORMATS: Record<string, string> = {
  txt: "txt", md: "markdown", markdown: "markdown", docx: "docx", pdf: "pdf",
};

export default function MaterialPanel({ csrf }: { csrf: string }) {
  const [mode, setMode] = useState<"note" | "file">("note");
  const [note, setNote] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [source, setSource] = useState<Source | null>(null);
  const [draft, setDraft] = useState<Preview | null>(null);
  const [choices, setChoices] = useState<Choices>(EMPTY);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const selection = useRef<HTMLTextAreaElement>(null);
  const picker = useRef<HTMLInputElement>(null);

  function resetPreview() {
    setSource(null);
    setDraft(null);
    setChoices(EMPTY);
    setStatus("");
    setError("");
  }

  async function run(task: () => Promise<void>) {
    setBusy(true);
    setError("");
    try {
      await task();
    } catch (reason) {
      setError(reason instanceof UploadError ? reason.message : "Не удалось связаться с сервером");
    } finally {
      setBusy(false);
    }
  }

  async function check(selected: Source, next: Choices) {
    const result = await send<Preview>(selected, next, csrf);
    setSource(selected);
    setChoices(next);
    setDraft(result);
    setStatus("");
  }

  async function start() {
    if (mode === "note") {
      return check({ format: "note", content: new Blob([note]) }, EMPTY);
    }
    const extension = file?.name.split(".").at(-1)?.toLowerCase() ?? "";
    if (!file || !Object.hasOwn(FORMATS, extension)) {
      throw new UploadError("Выберите файл TXT, Markdown, DOCX или PDF");
    }
    if (file.size > 20 * 1024 * 1024) throw new UploadError("Размер файла превышает 20 МБ");
    return check({ format: FORMATS[extension], content: file }, EMPTY);
  }

  function changeContact(id: string, enabled: boolean) {
    if (!source) return;
    const work = enabled ? [...choices.work, id] : choices.work.filter((entry) => entry !== id);
    void run(() => check(source, { work, hide: [] }));
  }

  function markSelection() {
    const field = selection.current;
    if (!field || !source || !draft) return;
    if (field.selectionStart === field.selectionEnd) {
      setError("Выделите текст, который нужно скрыть");
      return;
    }
    const start = toBase(draft.markers, [...draft.text.slice(0, field.selectionStart)].length, false);
    const end = toBase(draft.markers, [...draft.text.slice(0, field.selectionEnd)].length, true);
    void run(() => check(source, { ...choices, hide: [...choices.hide, { start, end }] }));
  }

  function highlight(marker: Mark) {
    const field = selection.current;
    if (!field || !draft) return;
    const chars = [...draft.text];
    field.focus();
    field.setSelectionRange(
      chars.slice(0, marker.start).join("").length,
      chars.slice(0, marker.end).join("").length,
    );
  }

  async function save() {
    if (!source || !draft) return;
    await send(source, choices, csrf, draft.proof);
    resetPreview();
    setNote("");
    setFile(null);
    if (picker.current) picker.current.value = "";
    setStatus("Очищенный материал сохранён как личный черновик");
  }

  const contacts = draft?.markers.filter((marker) => marker.contact_id) ?? [];

  return (
    <section className="material-panel">
      <h3>Добавить материал</h3>
      <label>
        Источник
        <select disabled={busy} value={mode} onChange={(event) => {
          setMode(event.target.value === "file" ? "file" : "note");
          setFile(null);
          resetPreview();
        }}>
          <option value="note">Заметка</option>
          <option value="file">Файл</option>
        </select>
      </label>
      {mode === "note" ? (
        <label>Текст заметки<textarea value={note} disabled={busy} maxLength={150_000}
          onChange={(event) => { setNote(event.target.value); resetPreview(); }} /></label>
      ) : (
        <label>Файл до 20 МБ<input type="file" ref={picker} disabled={busy}
          accept=".txt,.md,.markdown,.docx,.pdf" onChange={(event) => {
            setFile(event.target.files?.[0] ?? null);
            resetPreview();
          }} /></label>
      )}
      <button disabled={busy || (mode === "note" ? !note.trim() : !file)}
        onClick={() => void run(start)}>Проверить и очистить</button>
      {busy && <p role="status">Обрабатываем материал…</p>}
      {error && <p className="error" role="alert">{error}</p>}
      {status && <p role="status">{status}</p>}

      {draft && (
        <div className="material-preview">
          <h4>Предпросмотр перед сохранением</h4>
          <p>{draft.warning}</p>
          <label>
            Очищенный текст
            <textarea ref={selection} readOnly value={draft.text} rows={10} />
          </label>
          <button disabled={busy || choices.hide.length >= 24} onClick={markSelection}>
            Скрыть выделенное
          </button>
          {contacts.length > 0 && (
            <details>
              <summary>Скрытые внешние контакты ({contacts.length})</summary>
              {choices.hide.length > 0 && <p>Чтобы изменить контакты, запустите предпросмотр заново.</p>}
              {contacts.map((marker, index) => (
                <p key={marker.contact_id}>
                  <button disabled={busy} onClick={() => highlight(marker)}>
                    К контакту {index + 1} ({marker.label})
                  </button>
                  <button disabled={busy || choices.hide.length > 0 || choices.work.length >= 24}
                    onClick={() => marker.contact_id && changeContact(marker.contact_id, true)}>
                    Отметить рабочим
                  </button>
                </p>
              ))}
            </details>
          )}
          {choices.work.length > 0 && (
            <p>Рабочие внешние контакты
              {choices.work.map((id, index) => (
                <button key={id} disabled={busy || choices.hide.length > 0}
                  onClick={() => changeContact(id, false)}>Снова скрыть контакт {index + 1}</button>
              ))}
            </p>
          )}
          <button disabled={busy} onClick={() => void run(save)}>Сохранить очищенный текст</button>
        </div>
      )}
    </section>
  );
}

class UploadError extends Error {}

function toBase(markers: Mark[], position: number, isEnd: boolean) {
  let shift = 0;
  for (const marker of markers) {
    if (marker.base_start === null || marker.base_end === null) continue;
    if (position <= marker.start) return position + shift;
    if (position < marker.end) return isEnd ? marker.base_end : marker.base_start;
    shift = marker.base_end - marker.end;
  }
  return position + shift;
}

async function send<T>(source: Source, choices: Choices, csrf: string, proof?: string): Promise<T> {
  const headers = new Headers({
    "Content-Type": "application/octet-stream",
    "X-CSRF-Token": csrf,
    "X-Material-Format": source.format,
    "X-Material-Choices": JSON.stringify(choices),
  });
  if (proof) headers.set("X-Material-Proof", proof);
  const path = proof ? "/api/materials" : "/api/materials/preview";
  const response = await fetch(path, { method: "POST", headers, body: source.content, cache: "no-store" });
  if (!response.ok) {
    const error = await response.json().catch(() => null);
    throw new UploadError(error?.detail ?? "Не удалось обработать материал");
  }
  return (await response.json()) as T;
}
