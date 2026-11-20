import { useEffect, useRef, useState } from "react";

type Source = { chunk_id: string; material_id: string; text: string };
type Hit = { id: string; material_id: string; text: string };
type Turn = {
  id: string; question: string; message: string; segments: Source[];
  rating?: number; comment?: string;
};
type Chat = { id: string; created_at: string };
type Page<T> = { next_before: string | null } & T;
type Frame = {
  chat_id?: string; question?: string; turn_id?: string; message?: string;
  chunk_id?: string; material_id?: string; text?: string; detail?: string;
};

async function api(path: string, csrf: string, body?: object, signal?: AbortSignal) {
  const response = await fetch(`/api/${path}`, {
    cache: "no-store", method: body ? "POST" : "GET", signal,
    headers: body ? { "Content-Type": "application/json", "X-CSRF-Token": csrf } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) {
    const error = await response.json().catch(() => null);
    throw new Error(typeof error?.detail === "string" ? error.detail : "Не удалось выполнить запрос");
  }
  return response;
}

async function events(response: Response, receive: (event: string, data: Frame) => void) {
  if (!response.body) throw new Error("Ответ не получен");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const next = await reader.read();
      if (next.done) break;
      buffer += decoder.decode(next.value, { stream: true });
      let delimiter = buffer.indexOf("\n\n");
      while (delimiter !== -1) {
        const lines = buffer.slice(0, delimiter).split("\n");
        buffer = buffer.slice(delimiter + 2);
        const event = lines.find((line) => line.startsWith("event: "))?.slice(7);
        const data = lines.find((line) => line.startsWith("data: "))?.slice(6);
        if (event && data) receive(event, JSON.parse(data) as Frame);
        delimiter = buffer.indexOf("\n\n");
      }
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

function SourceLink({ id }: { id: string }) {
  return <a href={`/api/build/sources/${id}`} target="_blank" rel="noreferrer">Открыть источник</a>;
}

function Rating({ csrf, turn }: { csrf: string; turn: Turn }) {
  const [rating, setRating] = useState(turn.rating ?? null);
  const [comment, setComment] = useState(turn.comment ?? "");
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);
  async function save(value: number) {
    setBusy(true);
    setStatus("");
    try {
      const response = await api(`chat/turns/${turn.id}/feedback`, csrf, {
        rating: value, comment: comment || null,
      });
      const saved = await response.json() as { rating: number; comment: string | null };
      setRating(saved.rating);
      setComment(saved.comment ?? "");
      setStatus("Оценка сохранена");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Не удалось сохранить оценку");
    } finally { setBusy(false); }
  }
  return (
    <details>
      <summary>Оценить ответ {rating === 1 ? "👍" : rating === -1 ? "👎" : ""}</summary>
      <label>Комментарий <textarea maxLength={1000} disabled={busy} value={comment}
        onChange={(event) => setComment(event.target.value)} /></label>
      <button disabled={busy} onClick={() => void save(1)} aria-label="Полезный ответ">👍</button>
      <button disabled={busy} onClick={() => void save(-1)} aria-label="Неполезный ответ">👎</button>
      {status && <p role="status">{status}</p>}
    </details>
  );
}

export default function RagPanel({ csrf }: { csrf: string }) {
  const [query, setQuery] = useState("");
  const [scope, setScope] = useState("personal");
  const [hits, setHits] = useState<Hit[] | null>(null);
  const [semantic, setSemantic] = useState(true);
  const [chatId, setChatId] = useState<string | null>(null);
  const [chats, setChats] = useState<Chat[] | null>(null);
  const [chatCursor, setChatCursor] = useState<string | null>(null);
  const [turnCursor, setTurnCursor] = useState<string | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState<Turn | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [confirm, setConfirm] = useState(false);
  const active = useRef<AbortController | null>(null);
  useEffect(() => () => active.current?.abort(), []);

  async function run(task: (signal: AbortSignal) => Promise<void>) {
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setError("");
    setConfirm(false);
    try { await task(controller.signal); }
    catch (reason) {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Ошибка запроса");
    } finally {
      if (!controller.signal.aborted) { setBusy(false); setDraft(null); }
    }
  }

  async function search(signal: AbortSignal) {
    const result = await api("search", csrf, { question: query, scope }, signal);
    const data = await result.json() as { results: Hit[]; semantic_available: boolean };
    setHits(data.results);
    setSemantic(data.semantic_available);
  }

  async function ask(signal: AbortSignal) {
    setHits(null);
    const value: Turn = { id: "", question: "", message: "", segments: [] };
    const response = await api("chat/ask", csrf, { question: query, scope, chat_id: chatId }, signal);
    await events(response, (event, data) => {
      if (event === "error") throw new Error(data.detail ?? "Не удалось получить ответ");
      if (event === "started" && data.chat_id) {
        setChatId(data.chat_id);
        value.question = data.question ?? "";
        setDraft({ ...value });
      }
      if (event === "citation" && data.chunk_id && data.material_id && data.text) {
        value.segments.push({ chunk_id: data.chunk_id, material_id: data.material_id, text: data.text });
        setDraft({ ...value, segments: [...value.segments] });
      }
      if (event === "done") {
        value.id = data.turn_id ?? "";
        value.message = data.message ?? "";
      }
    });
    if (!value.id) throw new Error("Поток ответа прерван");
    setTurns((previous) => [...previous, value]);
    setQuery("");
    setChats(null);
  }

  async function list(signal: AbortSignal, before?: string) {
    const response = await api(before ? `chat?before=${before}` : "chat", csrf, undefined, signal);
    const page = await response.json() as Page<{ chats: Chat[] }>;
    setChats((previous) => before ? [...(previous ?? []), ...page.chats] : page.chats);
    setChatCursor(page.next_before);
  }

  async function open(id: string, signal: AbortSignal, before?: string) {
    const response = await api(`chat/${id}${before ? `?before=${before}` : ""}`, csrf, undefined, signal);
    const page = await response.json() as Page<{ turns: Turn[] }>;
    setTurns((previous) => before ? [...page.turns, ...previous] : page.turns);
    setTurnCursor(page.next_before);
    setChatId(id);
  }

  function reset() {
    setChatId(null);
    setTurns([]);
    setTurnCursor(null);
    setDraft(null);
    setConfirm(false);
  }

  return (
    <section className="material-panel">
      <h3>Поиск и вопросы по базе</h3>
      <form onSubmit={(event) => { event.preventDefault(); void run(search); }}>
        <label>Область <select disabled={busy} value={scope} onChange={(event) => {
          setScope(event.target.value); setHits(null);
        }}>
          <option value="personal">Моё</option>
          <option value="position">Моя должность</option>
          <option value="department">Мой отдел</option>
        </select></label>
        <label>Запрос <textarea required maxLength={1000} disabled={busy} value={query}
          onChange={(event) => setQuery(event.target.value)} /></label>
        <button disabled={busy || !query.trim()}>Найти</button>
        <button type="button" disabled={busy || !query.trim()} onClick={() => void run(ask)}>Спросить ИИ</button>
      </form>
      {error && <p className="error" role="alert">{error}</p>}
      {hits && (
        <div>
          {!semantic && <p>Доступен поиск по тексту.</p>}
          {hits.length === 0 && <p>В базе нет информации.</p>}
          {hits.map((hit) => <blockquote key={hit.id}>{hit.text} <SourceLink id={hit.id} /></blockquote>)}
        </div>
      )}
      <div>
        <button disabled={busy} onClick={() => void run((signal) => list(signal))}>История чатов</button>
        <button disabled={busy} onClick={reset}>Новый чат</button>
        {chatId && <button disabled={busy} onClick={() => setConfirm(true)}>Удалить чат</button>}
      </div>
      {confirm && chatId && (
        <div>
          <p>Удалить чат и все его ответы?</p>
          <button onClick={() => void run(async (signal) => {
            await api(`chat/${chatId}/delete`, csrf, {}, signal);
            reset(); setChats(null);
          })}>Подтвердить удаление</button>
          <button onClick={() => setConfirm(false)}>Отмена</button>
        </div>
      )}
      {chats && (
        <ul>
          {chats.length === 0 && <li>История пуста</li>}
          {chats.map((chat) => (
            <li key={chat.id}><button disabled={busy} onClick={() => void run((signal) => open(chat.id, signal))}>
              Чат от {new Date(chat.created_at).toLocaleString("ru-RU")}
            </button></li>
          ))}
          {chatCursor && <li><button disabled={busy} onClick={() => void run((signal) => list(signal, chatCursor))}>
            Ещё чаты
          </button></li>}
        </ul>
      )}
      {turnCursor && chatId && <button disabled={busy} onClick={() => void run((signal) => open(chatId, signal, turnCursor))}>
        Более ранние ответы
      </button>}
      {turns.map((turn) => <article key={turn.id}>
        <p><strong>{turn.question}</strong></p>
        <Answer turn={turn} />
        <Rating csrf={csrf} turn={turn} />
      </article>)}
      {draft && <article aria-live="polite">
        <p><strong>{draft.question}</strong></p><Answer turn={draft} />
        <p role="status">Ищем ответ по источникам…</p>
      </article>}
    </section>
  );
}

function Answer({ turn }: { turn: Turn }) {
  return (
    <>
      {turn.message && <p>{turn.message}</p>}
      {turn.segments.map((source, index) => (
        <blockquote key={`${source.chunk_id}-${index}`}>{source.text} <SourceLink id={source.chunk_id} /></blockquote>
      ))}
    </>
  );
}
