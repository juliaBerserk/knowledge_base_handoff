import { useState } from "react";

const ACTIONS: Record<string, string> = {
  login: "Вход", logout: "Выход", password_set: "Установка пароля",
  password_changed: "Смена пароля", totp_enabled: "Включение TOTP",
  invitation_issued: "Выпуск приглашения", admin_bootstrapped: "Создание первого администратора",
  knowledge_viewed: "Просмотр чужого объекта", material_uploaded: "Загрузка материала",
  material_redacted: "Очистка материала", knowledge_published: "Публикация",
  knowledge_submitted: "На проверку", knowledge_returned: "Возврат в черновики",
  knowledge_archived: "Архивирование", publication_settings_changed: "Настройки публикации",
  knowledge_copied: "Копирование", knowledge_exported: "Экспорт", ai_question: "Вопрос к ИИ",
  handoff_created: "Планирование эстафеты", handoff_cancelled: "Отмена эстафеты",
  handoff_activated: "Начало разбора", handoff_review_closed: "Закрытие разбора",
  handoff_conflict: "Конфликт назначения", employee_transferred: "Перевод сотрудника",
  account_created: "Создание учётной записи", accounts_imported: "Импорт CSV",
  department_created: "Создание отдела", position_created: "Создание должности",
  admin_settings_changed: "Настройки организации",
};
const OUTCOMES: Record<string, string> = {
  success: "Выполнено", denied: "Отказано", throttled: "Превышен лимит",
  challenge: "Требуется дополнительная проверка",
};
const TARGETS: Record<string, string> = {
  employee: "Сотрудник", material: "Материал", record: "Запись", space: "Пространство",
  chat: "Чат", handoff: "Эстафета", department: "Отдел", position: "Должность",
};
const MASKS: Record<string, string> = {
  password: "Пароль", token: "Токен", api_key: "Ключ API", private_key: "Приватный ключ",
  connection: "Строка подключения", card: "Карта", passport: "Паспорт", snils: "СНИЛС",
  inn: "ИНН", phone: "Телефон", address: "Адрес", email: "Почта", person: "ФИО",
  manual: "Скрыто вручную", confidential: "Конфиденциальное значение",
};

type Entry = {
  id: number; occurred_at: string; action: string; outcome: string;
  actor_id: string | null; target_kind: string | null; target_id: string | null;
  redaction_kind: string | null; redaction_count: number | null;
};
type Page = { events: Entry[]; next_before_id: number | null };

export default function AuditPanel() {
  const [action, setAction] = useState("");
  const [page, setPage] = useState<Page | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function load(before?: number) {
    setBusy(true);
    setError("");
    try {
      const params = new URLSearchParams();
      if (action) params.set("action", action);
      if (before !== undefined) params.set("before_id", String(before));
      const response = await fetch(`/api/audit/events?${params}`, { cache: "no-store" });
      if (!response.ok) {
        setPage(null);
        setError(response.status === 401 ? "Сессия завершена. Войдите снова." : "Журнал недоступен.");
        return;
      }
      setPage(await response.json() as Page);
    } catch {
      setPage(null);
      setError("Не удалось открыть журнал.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="material-panel" id="audit">
      <h3>Журнал аудита</h3>
      <form onSubmit={(event) => { event.preventDefault(); void load(); }}>
        <label>Действие <select aria-label="Действие" disabled={busy} value={action}
          onChange={(event) => { setAction(event.target.value); setPage(null); }}>
          <option value="">Все действия</option>
          {Object.entries(ACTIONS).map(([key, value]) => <option key={key} value={key}>{value}</option>)}
        </select></label>
        <button disabled={busy}>Показать события</button>
      </form>
      {busy && <p role="status">Загружаем журнал…</p>}
      {error && <p role="alert" className="error">{error}</p>}
      {page?.events.length === 0 && <p>Событий по этому фильтру нет.</p>}
      {page && page.events.length > 0 && (
        <div className="table-scroll" role="region" aria-label="События аудита" tabIndex={0}>
          <table>
            <caption>События в порядке поступления</caption>
            <thead><tr><th scope="col">Время</th><th scope="col">Действие</th>
              <th scope="col">Результат</th><th scope="col">Участник и объект</th></tr></thead>
            <tbody>{page.events.map((item) => <tr key={item.id}>
              <td><time dateTime={item.occurred_at}>{new Date(item.occurred_at).toLocaleString("ru-RU")}</time></td>
              <td>{ACTIONS[item.action] ?? "Событие"}
                {item.redaction_kind && <small>{MASKS[item.redaction_kind] ?? "Скрытие"} · {item.redaction_count}</small>}</td>
              <td>{OUTCOMES[item.outcome] ?? "—"}</td>
              <td>
                {item.actor_id ? <div>Участник <code>{item.actor_id}</code></div> : <div>Участник не указан</div>}
                {item.target_kind && item.target_id &&
                  <div>{TARGETS[item.target_kind] ?? "Объект"} <code>{item.target_id}</code></div>}
              </td>
            </tr>)}</tbody>
          </table>
        </div>
      )}
      {page?.next_before_id != null && (
        <button disabled={busy} onClick={() => void load(page.next_before_id ?? undefined)}>
          Более ранние события
        </button>
      )}
    </section>
  );
}
