import { useState } from "react";

const LABELS: Record<string, string> = {
  submit: "Отправить на проверку", return: "Вернуть в черновики",
  publish: "Опубликовать", archive: "В архив", copy: "Копировать к себе",
  draft: "Черновик", in_review: "На проверке", published: "Опубликовано", archived: "В архиве",
};

type Item = { id: string; author_id: string; status: string; actions: string[] };
type Listing = { items: Item[]; next_after_id: string | null };
type Detail = {
  id: string; text: string | null; author_id: string;
  original_id?: string | null; original_author_id?: string | null;
  source_chunk_id?: string | null; category?: string | null;
  confidence?: number | null; editable?: boolean;
};

const CATEGORIES: Record<string, string> = {
  responsibilities: "Обязанности", processes: "Процессы", cases: "Проблемы и решения",
  people: "Контакты", systems: "Системы", decisions: "Решения", unfinished: "Незавершённое",
  faqs: "Частые вопросы", glossary: "Глоссарий", risks: "Риски",
};

async function request<T>(path: string, csrf: string, body?: object): Promise<T> {
  const response = await fetch(`/api/${path}`, {
    cache: "no-store",
    method: body ? "POST" : "GET",
    headers: body ? { "Content-Type": "application/json", "X-CSRF-Token": csrf } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) {
    const error = await response.json().catch(() => null);
    throw new Error(error?.detail ?? "Не удалось выполнить запрос");
  }
  return (await response.json()) as T;
}

export default function PublicationPanel({ csrf, manager }: { csrf: string; manager: boolean }) {
  const [kind, setKind] = useState("materials");
  const [scope, setScope] = useState("personal");
  const [author, setAuthor] = useState("");
  const [page, setPage] = useState<Listing | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [source, setSource] = useState<string | null>(null);
  const [edit, setEdit] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const endpoint = `publication/${kind}`;
  const published = scope === "department" || scope === "position";

  async function run(task: () => Promise<void>) {
    setBusy(true);
    setError("");
    setNotice("");
    try { await task(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Не удалось выполнить запрос"); }
    finally { setBusy(false); }
  }

  async function load(after?: string) {
    const query = new URLSearchParams({ scope });
    if (published && author.trim()) query.set("author_id", author.trim());
    if (after) query.set("after_id", after);
    setDetail(null);
    setSource(null);
    setEdit(null);
    setPage(await request<Listing>(`${endpoint}?${query}`, csrf));
  }

  async function action(id: string, operation: string) {
    await request(`${endpoint}/${id}/${operation}`, csrf, {});
    await load();
    setNotice(operation === "copy" ? "Копия сохранена в личных записях" : "Статус обновлён");
  }

  async function open(id: string) {
    setDetail(await request<Detail>(`${endpoint}/${id}`, csrf));
    setSource(null);
    setEdit(null);
  }

  async function saveEdit(item: Detail, text: string) {
    const result = await request<{ text: string }>(`build/records/${item.id}/edit`, csrf, { text });
    setDetail({ ...item, text: result.text });
    setEdit(null);
    setNotice("Очищенная правка сохранена");
  }

  return (
    <section className="material-panel">
      <h3>Материалы и записи</h3>
      <form onSubmit={(event) => { event.preventDefault(); void run(() => load()); }}>
        <label>Область <select value={scope} disabled={busy} onChange={(event) => {
          setScope(event.target.value); setPage(null); setDetail(null);
        }}>
          <option value="personal">Моё</option>
          {manager && <option value="review">На проверке и разборе</option>}
          <option value="position">Моя должность</option>
          <option value="department">Мой отдел</option>
        </select></label>
        <label>Тип <select value={kind} disabled={busy} onChange={(event) => {
          setKind(event.target.value); setPage(null); setDetail(null);
        }}>
          <option value="materials">Материалы</option>
          <option value="records">Записи знаний</option>
        </select></label>
        {published && <label>Идентификатор автора <input value={author} disabled={busy}
          onChange={(event) => { setAuthor(event.target.value); setPage(null); setDetail(null); }} /></label>}
        <button disabled={busy}>Показать</button>
      </form>
      {error && <p className="error" role="alert">{error}</p>}
      {notice && <p role="status">{notice}</p>}
      {page?.items.length === 0 && <p>В этой области нет доступных объектов.</p>}
      {page?.items.map((item) => (
        <div key={item.id}>
          <p>{LABELS[item.status]} · <code>{item.id}</code></p>
          <button disabled={busy} onClick={() => void run(() => open(item.id))}>Открыть</button>
          {item.actions.map((operation) => (
            <button key={operation} disabled={busy} onClick={() => void run(() => action(item.id, operation))}>
              {LABELS[operation]}
            </button>
          ))}
        </div>
      ))}
      {page?.next_after_id && <button disabled={busy}
        onClick={() => void run(() => load(page.next_after_id ?? undefined))}>Следующая страница</button>}
      {detail && (
        <section>
          <h4>Содержимое</h4>
          <p>Автор <code>{detail.author_id}</code></p>
          {detail.category && <p>{CATEGORIES[detail.category]}</p>}
          {typeof detail.confidence === "number" && <p>Уверенность модели {Math.round(detail.confidence * 100)}%</p>}
          <pre className="knowledge-text">{detail.text ?? "Текста нет"}</pre>
          {detail.text !== null && (
            <p><a href={`/api/${endpoint}/${detail.id}/export`} download>Скачать очищенный текст</a></p>
          )}
          {detail.source_chunk_id && (
            <button disabled={busy} onClick={() => void run(async () => {
              const result = await request<{ text: string }>(`build/sources/${detail.source_chunk_id}`, csrf);
              setSource(result.text);
            })}>Показать фрагмент-источник</button>
          )}
          {source && <blockquote className="knowledge-text">{source}</blockquote>}
          {detail.editable && edit === null && (
            <button disabled={busy} onClick={() => setEdit(detail.text ?? "")}>Править запись</button>
          )}
          {detail.editable && edit !== null && (
            <form onSubmit={(event) => { event.preventDefault(); void run(() => saveEdit(detail, edit)); }}>
              <label>Текст записи <textarea required maxLength={12000} value={edit}
                onChange={(event) => setEdit(event.target.value)} /></label>
              <button disabled={busy}>Сохранить правку</button>
            </form>
          )}
          {detail.original_id && (
            <div>
              <p>Оригинал <code>{detail.original_id}</code> · Автор оригинала <code>{detail.original_author_id}</code></p>
              <button disabled={busy} onClick={() => {
                const original = detail.original_id;
                if (original) void run(() => open(original));
              }}>Открыть оригинал</button>
            </div>
          )}
          <button disabled={busy} onClick={() => setDetail(null)}>Закрыть</button>
        </section>
      )}
    </section>
  );
}
