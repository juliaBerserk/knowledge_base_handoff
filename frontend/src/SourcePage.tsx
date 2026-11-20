import { useEffect, useState } from "react";

const UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}";
const CHUNK_PATH = new RegExp(`^/sources/(${UUID})/?$`, "i");
const ITEM_PATH = new RegExp(`^/knowledge/(materials|records)/(${UUID})/?$`, "i");

type Route = { kind: "chunk" | "materials" | "records"; id: string };
type Source = {
  text: string | null;
  material_id?: string;
  author_id?: string;
  status?: string;
};

const STATUSES: Record<string, string> = {
  draft: "Черновик", in_review: "На проверке", published: "Опубликовано", archived: "В архиве",
};

export function sourceRoute(path: string): Route | null {
  const chunk = CHUNK_PATH.exec(path);
  if (chunk) return { kind: "chunk", id: chunk[1] };
  const item = ITEM_PATH.exec(path);
  if (!item) return null;
  return { kind: item[1].toLowerCase() === "materials" ? "materials" : "records", id: item[2] };
}

export default function SourcePage({ route }: { route: Route }) {
  const { kind, id } = route;
  const [source, setSource] = useState<Source | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const chunk = kind === "chunk";
  const endpoint = chunk ? `/api/build/sources/${id}` : `/api/publication/${kind}/${id}`;

  useEffect(() => {
    const controller = new AbortController();
    fetch(endpoint, { cache: "no-store", signal: controller.signal }).then(async (response) => {
      if (!response.ok) {
        if (response.status === 401) {
          setError("Войдите в систему, чтобы открыть источник.");
        } else if (response.status === 403 || response.status === 404) {
          setError("Источник недоступен для вашей учётной записи.");
        } else {
          setError("Не удалось открыть источник.");
        }
        return;
      }
      const item = await response.json() as Source;
      if (!controller.signal.aborted) setSource(item);
    }).catch(() => {
      if (!controller.signal.aborted) {
        setError("Не удалось открыть источник.");
      }
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false);
    });
    return () => controller.abort();
  }, [endpoint]);

  return (
    <section className="card source-page">
      <h2>{chunk ? "Фрагмент источника" : kind === "materials" ? "Материал" : "Запись знаний"}</h2>
      <a href="/">На главную</a>
      {loading && <p role="status">Загружаем источник…</p>}
      {error && <p role="alert" className="error">{error}</p>}
      {source && (
        <>
          {source.status && <p>{STATUSES[source.status] ?? ""}</p>}
          {source.author_id && <p>Автор <code>{source.author_id}</code></p>}
          <pre className="knowledge-text">{source.text ?? "В источнике нет текста."}</pre>
          {chunk && source.material_id && (
            <a href={`/knowledge/materials/${source.material_id}`}>Открыть материал целиком</a>
          )}
          {!chunk && source.text !== null && (
            <a href={`${endpoint}/export`} download>Скачать очищенный текст</a>
          )}
        </>
      )}
    </section>
  );
}
