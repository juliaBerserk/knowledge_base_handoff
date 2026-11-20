import { useId, useState, type FormEvent } from "react";

type Handoff = {
  id: string; kind: "departure" | "transfer"; status: string;
  employee_id: string; position_id: string; effective_at: string; review_until: string | null;
  employee_name: string; position_title: string; successor_name: string | null;
  successor_id: string | null; target_position_id: string | null; successor_assigned: boolean;
  issue: string | null;
};
type Detail = Handoff & {
  completeness: Record<string, number> | null; can_cancel: boolean; can_start: boolean;
};
type Options = {
  employees: { id: string; name: string; position_id: string | null;
    position_title: string | null; can_succeed: boolean }[];
  positions: { id: string; title: string; department: string }[];
};
type Hint = { id: string; kind: string; category: string | null; excerpt: string | null };

const STATUSES: Record<string, string> = {
  scheduled: "Запланировано", review: "На разборе", completed: "Завершено",
  conflict: "Требуется уточнение назначения", cancelled: "Отменено",
};
const CATEGORIES: Record<string, string> = {
  responsibilities: "Обязанности", processes: "Процессы", cases: "Проблемы и решения",
  people: "Контакты", systems: "Системы", decisions: "Решения",
  unfinished: "Незавершённое", faqs: "Частые вопросы", glossary: "Глоссарий", risks: "Риски",
};
const ISSUES: Record<string, string> = {
  assignment_changed: "Исходное назначение изменилось. Проверьте данные и создайте новый план.",
  successor_changed: "Преемник не назначен. Проверьте его текущее назначение.",
};

async function api<T>(path: string, csrf: string, body?: object): Promise<T> {
  const response = await fetch(`/api/handoffs${path}`, {
    cache: "no-store", method: body ? "POST" : "GET",
    headers: body ? { "Content-Type": "application/json", "X-CSRF-Token": csrf } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail ?? "Не удалось выполнить запрос");
  return data as T;
}

export default function HandoffPanel({ csrf, role }: { csrf: string; role: string }) {
  const manages = role === "hr" || role === "department_head";
  const employeeLabel = useId();
  const successorLabel = useId();
  const [items, setItems] = useState<Handoff[] | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [options, setOptions] = useState<Options | null>(null);
  const [person, setPerson] = useState("");
  const [operation, setOperation] = useState("departure");
  const [hints, setHints] = useState<Hint[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [confirmCancel, setConfirmCancel] = useState(false);

  async function run(task: () => Promise<void>) {
    setBusy(true);
    setError("");
    setNotice("");
    try { await task(); }
    catch (reason) {
      setError(reason instanceof Error ? reason.message : "Не удалось выполнить запрос");
    } finally { setBusy(false); }
  }

  async function list(before?: string) {
    const page = await api<{ items: Handoff[]; next_before: string | null }>(
      before ? `?before=${before}` : "", csrf,
    );
    setItems((previous) => before ? [...(previous ?? []), ...page.items] : page.items);
    setCursor(page.next_before);
  }

  async function open(id: string) {
    setDetail(await api<Detail>(`/${id}`, csrf));
    setHints(null);
    setConfirmCancel(false);
  }

  function schedule(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const selected = options?.employees.find((employee) => employee.id === person);
    const cutoff = new Date(`${form.get("last_day")}T${form.get("time")}`);
    void run(async () => {
      if (!selected?.position_id || Number.isNaN(cutoff.valueOf())) {
        throw new Error("Выберите сотрудника и укажите дату и время");
      }
      const result = await api<{ id: string }>("", csrf, {
        employee_id: selected.id, position_id: selected.position_id,
        effective_at: cutoff.toISOString(), successor_id: form.get("successor") || null,
        target_position_id: form.get("destination") || null,
      });
      setOptions(null);
      setPerson("");
      await list();
      await open(result.id);
      setNotice("План сохранён");
    });
  }

  return (
    <section className="material-panel">
      <h3>Эстафеты</h3>
      <button disabled={busy} onClick={() => void run(async () => {
        await list();
        if (detail) await open(detail.id);
      })}>Обновить список</button>
      {manages && !options && (
        <button disabled={busy} onClick={() => void run(async () => {
          setOptions(await api<Options>("/options", csrf));
        })}>Создать эстафету или перевод</button>
      )}
      {error && <p className="error" role="alert">{error}</p>}
      {notice && <p role="status">{notice}</p>}
      {options && (
        <form onSubmit={schedule}>
          <label>Действие <select value={operation} disabled={busy}
            onChange={(event) => setOperation(event.target.value)}>
            <option value="departure">Увольнение</option><option value="transfer">Перевод</option>
          </select></label>
          <label><span id={employeeLabel}>Сотрудник</span> <select aria-labelledby={employeeLabel}
            required disabled={busy} value={person}
            onChange={(event) => setPerson(event.target.value)}>
            <option value="">Выберите сотрудника</option>
            {options.employees.filter((employee) => employee.position_id).map((employee) =>
              <option key={employee.id} value={employee.id}>{employee.name} · {employee.position_title}</option>)}
          </select></label>
          {operation === "departure" ? (
            <label><span id={successorLabel}>Преемник</span> <select aria-labelledby={successorLabel}
              name="successor" disabled={busy}>
              <option value="">Оставить должность вакантной</option>
              {options.employees.filter((employee) => employee.can_succeed && employee.id !== person)
                .map((employee) => <option key={employee.id} value={employee.id}>{employee.name}</option>)}
            </select></label>
          ) : (
            <label>Новая должность <select name="destination" required disabled={busy}>
              <option value="">Выберите должность</option>
              {options.positions.filter((position) => position.id !== options.employees.find(
                (employee) => employee.id === person,
              )?.position_id).map((position) =>
                <option key={position.id} value={position.id}>{position.title} · {position.department}</option>)}
            </select></label>
          )}
          <label>{operation === "departure" ? "Последний рабочий день" : "Дата перевода"}
            <input name="last_day" type="date" required disabled={busy} /></label>
          <label>Время {operation === "departure" ? "блокировки" : "перевода"} по часовому поясу устройства
            <input name="time" type="time" required disabled={busy} /></label>
          <button disabled={busy}>Запланировать</button>
          <button type="button" disabled={busy} onClick={() => setOptions(null)}>Закрыть форму</button>
        </form>
      )}
      {items?.length === 0 && <p>Доступных эстафет пока нет.</p>}
      {items && (
        <ul>{items.map((item) => (
          <li key={item.id}><button disabled={busy} onClick={() => void run(() => open(item.id))}>
            {item.employee_name} · {item.kind === "departure" ? "Увольнение" : "Перевод"} · {labelDate(item.effective_at)} · {STATUSES[item.status]}
          </button></li>
        ))}</ul>
      )}
      {cursor && <button disabled={busy} onClick={() => void run(() => list(cursor))}>Ранее</button>}
      {detail && (
        <section>
          <h4>{detail.kind === "departure" ? "Эстафета при увольнении" : "Перевод"}</h4>
          <p>{STATUSES[detail.status]} · {labelDate(detail.effective_at)}</p>
          <p>Сотрудник {detail.employee_name} · {detail.position_title}</p>
          {detail.successor_name && <p>Преемник {detail.successor_name}</p>}
          {detail.review_until && <p>Разбор до {labelDate(detail.review_until)}</p>}
          {detail.issue && <p className="error">{ISSUES[detail.issue]}</p>}
          {detail.status === "review" && role === "department_head" &&
            <p>Для разбора откройте область «На проверке и разборе» в материалах и записях.</p>}
          {detail.completeness && (
            <>
              <h5>Полнота по категориям</h5>
              <ul>{Object.entries(detail.completeness).map(([category, count]) => (
                <li key={category}>{CATEGORIES[category] ?? category} · {count || "Записей пока нет"}</li>
              ))}</ul>
            </>
          )}
          {detail.can_start && (
            <button disabled={busy} onClick={() => void run(async () => {
              const start = await api<{ items: Hint[] }>(`/${detail.id}/start`, csrf);
              setHints(start.items);
            })}>С чего начать</button>
          )}
          {hints && (
            <ul>{hints.length === 0 ? <li>Доступных публикаций пока нет.</li> : hints.map((item) => (
              <li key={item.id}>
                <p>{item.category ? CATEGORIES[item.category] : "Материал"} · {item.excerpt}</p>
                <a href={`/api/publication/${item.kind}/${item.id}`} target="_blank" rel="noreferrer">
                  Открыть публикацию
                </a>
              </li>
            ))}</ul>
          )}
          {detail.can_cancel && detail.status === "scheduled" && (
            confirmCancel ? <button disabled={busy} onClick={() => void run(async () => {
              await api(`/${detail.id}/cancel`, csrf, {});
              await list();
              await open(detail.id);
            })}>Подтвердить отмену плана</button> : (
              <button disabled={busy} onClick={() => setConfirmCancel(true)}>Отменить план</button>
            )
          )}
        </section>
      )}
    </section>
  );
}

function labelDate(value: string) {
  return new Date(value).toLocaleString("ru-RU", { dateStyle: "medium", timeStyle: "short" });
}
