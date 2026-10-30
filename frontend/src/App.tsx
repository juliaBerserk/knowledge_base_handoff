import { useEffect, useState } from "react";

type Health = {
  ok: boolean;
  service: string;
  database: boolean;
  vector_extension: boolean;
};

type HealthState = "loading" | "ok" | "error";

export default function App() {
  const [state, setState] = useState<HealthState>("loading");
  const [health, setHealth] = useState<Health | null>(null);
  const [errorText, setErrorText] = useState("");

  useEffect(() => {
    let cancelled = false;
    fetch("/api/health")
      .then(async (response) => {
        if (!response.ok) {
          throw new Error("Сервис временно недоступен");
        }
        return (await response.json()) as Health;
      })
      .then((data) => {
        if (!cancelled) {
          setHealth(data);
          setState("ok");
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setState("error");
          setErrorText(error instanceof Error ? error.message : "Неизвестная ошибка");
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <main className="page">
      <header>
        <p className="eyebrow">Корпоративные знания</p>
        <h1>Эстафета</h1>
        <p className="lead">
          Система сохранения и передачи знаний. Каркас запущен, рабочие сценарии появятся
          на следующих этапах.
        </p>
      </header>
      <section className="card" aria-live="polite">
        <h2>Состояние сервиса</h2>
        {state === "loading" && <p>Проверяем API…</p>}
        {state === "error" && (
          <p className="error">
            API недоступен. {errorText}. Запустите backend или весь стек Docker Compose.
          </p>
        )}
        {state === "ok" && health && (
          <ul className="status">
            <li>Сервис: {health.service}</li>
            <li>База данных: {health.database ? "доступна" : "нет"}</li>
            <li>Расширение pgvector: {health.vector_extension ? "установлено" : "нет"}</li>
          </ul>
        )}
      </section>
    </main>
  );
}
