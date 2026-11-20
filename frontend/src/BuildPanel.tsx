import { useEffect, useState } from "react";

type Job = {
  id: string;
  status: string;
  stage: string;
  progress: {
    total?: number;
    built?: number;
    skipped?: number;
    failed?: number;
    errors?: Record<string, string>;
    error?: string;
  };
};

const STAGES: Record<string, string> = {
  queued: "В очереди", planning: "Проверяем материалы", chunking: "Делим текст на фрагменты",
  embedding: "Считаем эмбеддинги", extracting: "Извлекаем записи", saving: "Сохраняем результат",
};
const RESULTS: Record<string, string> = {
  succeeded: "Сборка завершена", partial: "Сборка завершена с ошибками", failed: "Сборка не удалась",
};
const ERRORS: Record<string, string> = {
  provider: "Ошибка модели", storage: "Ошибка сохранения", interrupted: "Воркер прервал обработку",
  access_revoked: "Доступ сотрудника закрыт",
};

export default function BuildPanel({ csrf }: { csrf: string }) {
  const [job, setJob] = useState<Job | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const active = job?.status === "queued" || job?.status === "running";

  useEffect(() => {
    if (busy) return;
    const controller = new AbortController();
    async function load() {
      try {
        const response = await fetch("/api/build", { cache: "no-store", signal: controller.signal });
        if (!response.ok) throw new Error();
        const result = await response.json() as { job: Job | null };
        if (!controller.signal.aborted) {
          setJob(result.job);
          setError("");
        }
      } catch {
        if (!controller.signal.aborted) setError("Не удалось обновить статус задачи");
      }
    }
    void load();
    const interval = active ? window.setInterval(() => void load(), 2500) : undefined;
    return () => { controller.abort(); window.clearInterval(interval); };
  }, [active, busy]);

  async function start() {
    setBusy(true);
    setError("");
    try {
      const response = await fetch("/api/build", {
        method: "POST", body: "{}", headers: {
          "Content-Type": "application/json", "X-CSRF-Token": csrf,
        },
      });
      if (!response.ok) throw new Error();
      const result = await response.json() as { id: string };
      setJob({ id: result.id, status: "queued", stage: "queued", progress: {} });
    } catch {
      setError("Не удалось поставить сборку в очередь");
    } finally { setBusy(false); }
  }

  return (
    <section className="material-panel">
      <h3>Сборка базы</h3>
      <button disabled={busy || active} onClick={() => void start()}>Собрать базу</button>
      {error && <p className="error" role="alert">{error}</p>}
      {job && (
        <>
          <p role="status">{RESULTS[job.status] ?? STAGES[job.stage] ?? job.stage}</p>
          {job.progress.total !== undefined && (
            <p>Всего {job.progress.total} · Собрано {job.progress.built ?? 0} ·
              Пропущено {job.progress.skipped ?? 0} · Ошибок {job.progress.failed ?? 0}</p>
          )}
          {job.progress.error && <p className="error">{ERRORS[job.progress.error]}</p>}
          {Object.keys(job.progress.errors ?? {}).length > 0 && (
            <details><summary>Материалы с ошибками</summary>
              <ul>{Object.entries(job.progress.errors ?? {}).map(([id, code]) => (
                <li key={id}>{ERRORS[code]} · <code>{id}</code></li>
              ))}</ul>
            </details>
          )}
        </>
      )}
    </section>
  );
}
