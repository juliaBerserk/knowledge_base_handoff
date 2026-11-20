import { useRef, useState, type FormEvent } from "react";

type Unit = { id: string; name: string };
type Position = { id: string; department_id: string; title: string };
type Org = { departments: Unit[]; positions: Position[] };
type Account = {
  id: string; full_name: string; corporate_email: string | null;
  role: string; department: string | null; position: string | null; status: string;
};
type Invitation = { row: number; id: string; sent: boolean; url: string | null };
type Settings = { review_required: boolean; handoff_retention: "archive" | "delete" };
type RowError = { row: number; detail: string };
type Tab = "accounts" | "org" | "import" | "settings";
const EMPTY: Org = { departments: [], positions: [] };
const ROLES: Record<string, string> = {
  employee: "Сотрудник", department_head: "Руководитель отдела", hr: "HR",
  admin: "Администратор", security_auditor: "Аудитор ИБ",
};
const STATUSES: Record<string, string> = {
  active: "Активна", invited: "Приглашение выпущено", pending: "Ожидает приглашения",
  blocked: "Заблокирована",
};

class AdminError extends Error {
  constructor(message: string, readonly rows: RowError[] = []) { super(message); }
}

async function api<T>(path: string, csrf: string, body?: object): Promise<T> {
  const response = await fetch(`/api/${path}`, {
    cache: "no-store", method: body ? "POST" : "GET",
    headers: body ? { "Content-Type": "application/json", "X-CSRF-Token": csrf } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new AdminError(data.detail ?? "Не удалось выполнить запрос", data.errors);
  return data as T;
}

export default function AdminPanel({ csrf }: { csrf: string }) {
  const [tab, setTab] = useState<Tab | null>(null);
  const [org, setOrg] = useState<Org>(EMPTY);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [csv, setCsv] = useState("");
  const csvFile = useRef<HTMLInputElement>(null);
  const [count, setCount] = useState<number | null>(null);
  const [sendEmail, setSendEmail] = useState(true);
  const [invitations, setInvitations] = useState<Invitation[]>([]);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [issueFor, setIssueFor] = useState<Account | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [rowErrors, setRowErrors] = useState<RowError[]>([]);
  const [notice, setNotice] = useState("");

  async function run(task: () => Promise<void>) {
    setBusy(true); setError(""); setRowErrors([]); setNotice("");
    try { await task(); }
    catch (reason) {
      setError(reason instanceof Error ? reason.message : "Не удалось выполнить запрос");
      if (reason instanceof AdminError) setRowErrors(reason.rows);
    } finally { setBusy(false); }
  }

  async function loadOrg() { setOrg(await api<Org>("admin/org", csrf)); }

  async function list(query = search, before?: string) {
    const params = new URLSearchParams({ q: query });
    if (before) params.set("before", before);
    const data = await api<{ items: Account[]; next_before: string | null }>(`admin/accounts?${params}`, csrf);
    setAccounts((previous) => before ? [...previous, ...data.items] : data.items);
    setCursor(data.next_before);
  }

  function open(next: Tab) {
    setTab(next);
    setIssueFor(null);
    void run(async () => {
      if (next === "accounts") await Promise.all([loadOrg(), list()]);
      if (next === "org") await loadOrg();
      if (next === "settings") setSettings(await api<Settings>("admin/settings", csrf));
    });
  }

  function submit(event: FormEvent<HTMLFormElement>, task: (data: FormData) => Promise<void>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    void run(async () => { await task(data); form.reset(); });
  }

  async function loadCsv(file: File | undefined) {
    setCount(null); setCsv(""); setInvitations([]);
    if (!file) return;
    if (file.size > 128 * 1024) throw new Error("Файл CSV превышает 128 КиБ");
    try {
      setCsv(new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer()));
    } catch { throw new Error("Файл должен быть в кодировке UTF-8"); }
  }

  return (
    <section className="material-panel">
      <h3>Администрирование</h3>
      <div>
        <button disabled={busy} onClick={() => open("accounts")}>Учётные записи</button>
        <button disabled={busy} onClick={() => open("org")}>Отделы и должности</button>
        <button disabled={busy} onClick={() => open("import")}>Импорт CSV</button>
        <button disabled={busy} onClick={() => open("settings")}>Настройки</button>
      </div>
      {error && <p className="error" role="alert">{error}</p>}
      {notice && <p role="status">{notice}</p>}
      {rowErrors.length > 0 && <ul>{rowErrors.map((item, index) =>
        <li key={index}>Строка {item.row} · {item.detail}</li>)}</ul>}

      {tab === "org" && (
        <>
          <form onSubmit={(event) => submit(event, async (data) => {
            await api("admin/departments", csrf, { name: data.get("name") });
            await loadOrg();
            setNotice("Отдел создан");
          })}>
            <label>Название отдела <input name="name" maxLength={200} required disabled={busy} /></label>
            <button disabled={busy}>Создать отдел</button>
          </form>
          <form onSubmit={(event) => submit(event, async (data) => {
            await api("admin/positions", csrf, { department_id: data.get("department"), title: data.get("title") });
            await loadOrg();
            setNotice("Должность создана");
          })}>
            <label>Отдел <select name="department" aria-label="Отдел" required disabled={busy}>
              <option value="">Выберите отдел</option>
              {org.departments.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
            </select></label>
            <label>Название должности <input name="title" maxLength={200} required disabled={busy} /></label>
            <button disabled={busy || !org.departments.length}>Создать должность</button>
          </form>
          {org.departments.map((unit) => <details key={unit.id}>
            <summary>{unit.name}</summary>
            <p>UUID отдела <code>{unit.id}</code></p>
            <ul>{org.positions.filter((position) => position.department_id === unit.id).map((position) =>
              <li key={position.id}>{position.title} · <code>{position.id}</code></li>)}</ul>
          </details>)}
        </>
      )}

      {tab === "accounts" && (
        <>
          <h4>Новая учётная запись</h4>
          {!busy && !org.positions.length && <p>Создайте отдел и должность в справочнике.</p>}
          <form onSubmit={(event) => submit(event, async (data) => {
            const result = await api<Invitation>("admin/accounts", csrf, {
              full_name: data.get("full_name"), corporate_email: data.get("email"),
              role: data.get("role"), position_id: data.get("position"), send_email: data.has("send"),
            });
            setInvitations([result]);
            setNotice("Учётная запись создана");
            await list();
          })}>
            <label>ФИО <input name="full_name" required maxLength={255} disabled={busy} /></label>
            <label>Корпоративная почта <input name="email" type="email" maxLength={254} disabled={busy} /></label>
            <label>Должность <select name="position" aria-label="Должность" required disabled={busy}>
              <option value="">Выберите должность</option>
              {org.positions.map((position) => <option key={position.id} value={position.id}>
                {org.departments.find((unit) => unit.id === position.department_id)?.name} · {position.title} · {position.id.slice(0, 8)}
              </option>)}
            </select></label>
            <label>Роль <select name="role" aria-label="Роль" disabled={busy}>
              {Object.entries(ROLES).map(([key, value]) => <option key={key} value={key}>{value}</option>)}
            </select></label>
            <label><input type="checkbox" name="send" defaultChecked disabled={busy} /> Отправить приглашение письмом</label>
            <button disabled={busy || !org.positions.length}>Создать учётную запись</button>
          </form>
          <h4>Справочник сотрудников</h4>
          <form onSubmit={(event) => { event.preventDefault(); void run(() => list()); }}>
            <label>Поиск по имени или почте <input value={search} disabled={busy} maxLength={120}
              onChange={(event) => setSearch(event.target.value)} /></label>
            <button disabled={busy}>Найти учётки</button>
          </form>
          <ul>{accounts.map((account) => <li key={account.id}>
            <strong>{account.full_name}</strong> · {ROLES[account.role]} · {STATUSES[account.status]}
            <p>{account.department ?? "Без отдела"} · {account.position ?? "Без должности"}</p>
            <p>{account.corporate_email ?? "Без почты"} · <code>{account.id}</code></p>
            {account.status !== "blocked" && <button disabled={busy}
              onClick={() => setIssueFor(account)}>Приглашение или восстановление</button>}
          </li>)}</ul>
          {cursor && <button disabled={busy} onClick={() => void run(() => list(search, cursor))}>Следующая страница</button>}
          {issueFor && <form onSubmit={(event) => submit(event, async (data) => {
            const issued = await api<{ url: string; delivered: boolean }>(
              `auth/invitations/${issueFor.id}`, csrf, { send_email: data.has("deliver") },
            );
            setInvitations([{ row: 1, id: issueFor.id, sent: issued.delivered,
              url: issued.delivered ? null : issued.url }]);
            setIssueFor(null);
            setNotice("Новое приглашение выпущено");
            await list();
          })}>
            <p>Новое приглашение для {issueFor.full_name} заменит предыдущую ссылку.</p>
            <label><input type="checkbox" name="deliver" defaultChecked disabled={busy} /> Отправить письмом</label>
            <button disabled={busy}>Выпустить ссылку</button>
            <button type="button" disabled={busy} onClick={() => setIssueFor(null)}>Отмена</button>
          </form>}
        </>
      )}

      {tab === "import" && (
        <>
          <h4>Импорт учётных записей</h4>
          <p>CSV в UTF‑8, до 300 строк. Колонки: ФИО, корпоративная почта, отдел, должность, роль.</p>
          <label>Файл CSV <input ref={csvFile} type="file" accept=".csv,text/csv" disabled={busy} onChange={(event) => {
            const file = event.target.files?.[0];
            void run(() => loadCsv(file));
          }} /></label>
          <label><input type="checkbox" checked={sendEmail} disabled={busy}
            onChange={(event) => setSendEmail(event.target.checked)} /> Отправить приглашения письмом</label>
          <button disabled={busy || !csv} onClick={() => void run(async () => {
            setCount(null);
            const data = await api<{ count: number }>("admin/accounts/import/preview", csrf, { csv_text: csv });
            setCount(data.count);
          })}>Проверить CSV</button>
          {count !== null && <>
            <p>Готово к импорту {count} учётных записей.</p>
            <button disabled={busy} onClick={() => void run(async () => {
              setInvitations([]);
              const data = await api<{ created: number; accounts: Invitation[] }>(
                "admin/accounts/import", csrf, { csv_text: csv, send_email: sendEmail },
              );
              setCount(null); setCsv(""); setInvitations(data.accounts);
              if (csvFile.current) csvFile.current.value = "";
              setNotice(`Импортировано учётных записей: ${data.created}`);
            })}>Импортировать</button>
          </>}
        </>
      )}

      {tab === "settings" && settings && (
        <form onSubmit={(event) => {
          event.preventDefault();
          void run(async () => {
            setSettings(await api<Settings>("admin/settings", csrf, settings));
            setNotice("Настройки сохранены");
          });
        }}>
          <label><input type="checkbox" checked={settings.review_required} disabled={busy}
            onChange={(event) => setSettings({ ...settings, review_required: event.target.checked })} /> Обязательная проверка публикации руководителем</label>
          <label>После 30 дней разбора <select aria-label="Политика хранения" value={settings.handoff_retention}
            disabled={busy} onChange={(event) => setSettings({
              ...settings, handoff_retention: event.target.value === "delete" ? "delete" : "archive",
            })}>
            <option value="archive">Архивировать личное пространство</option>
            <option value="delete">Удалить личное пространство</option>
          </select></label>
          <p>Политика применяется к новым эстафетам.</p>
          <button disabled={busy}>Сохранить настройки</button>
        </form>
      )}

      {invitations.length > 0 && <>
        <h4>Приглашения на 72 часа</h4>
        <ul>{invitations.map((item) => <li key={item.id}>
          Строка {item.row} · <code>{item.id}</code> · {item.sent ? "Письмо отправлено" : "Передайте ссылку лично"}
          {item.url && <label>Одноразовая ссылка <input readOnly value={item.url}
            onFocus={(event) => event.target.select()} /></label>}
        </li>)}</ul>
        <button onClick={() => setInvitations([])}>Скрыть приглашения</button>
      </>}
    </section>
  );
}
