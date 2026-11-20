import { useEffect, useState, type FormEvent } from "react";
import MaterialPanel from "./MaterialPanel";
import BuildPanel from "./BuildPanel";
import PublicationPanel from "./PublicationPanel";
import AdminPanel from "./AdminPanel";
import RagPanel from "./RagPanel";
import HandoffPanel from "./HandoffPanel";

type Session = {
  employee_id: string;
  role: string;
  csrf_token: string;
  enrollment_only: boolean;
  totp_enabled: boolean;
};

class AuthError extends Error {
  constructor(message: string, readonly needsCode = false, readonly status = 0) {
    super(message);
  }
}

async function auth<T>(path: string, body?: object, csrf?: string): Promise<T> {
  const headers = new Headers();
  if (body) headers.set("Content-Type", "application/json");
  if (csrf) headers.set("X-CSRF-Token", csrf);
  const response = await fetch(`/api/auth/${path}`, {
    method: body ? "POST" : "GET",
    headers,
    body: body ? JSON.stringify(body) : undefined,
    cache: "no-store",
  });
  if (!response.ok) {
    const error = await response.json().catch(() => null);
    throw new AuthError(
      error?.detail ?? "Сервис аутентификации временно недоступен",
      error?.totp_required === true,
      response.status,
    );
  }
  return (await response.json()) as T;
}

function field(form: FormData, name: string) {
  return String(form.get(name) ?? "");
}

const MIN_PASSWORD_LENGTH = 16;
const PASSWORD_HINT = "Минимум 16 символов. Можно сочетать кириллицу, латиницу, цифры и спецсимволы.";

function checkNewPassword(password: string) {
  if ([...password].length < MIN_PASSWORD_LENGTH) {
    throw new AuthError("Новый пароль должен содержать не менее 16 символов");
  }
}

export default function AuthPanel() {
  const [invite, setInvite] = useState(() =>
    new URLSearchParams(window.location.hash.slice(1)).get("invite"),
  );
  const [session, setSession] = useState<Session | null>(null);
  const [loginId, setLoginId] = useState("");
  const [needsCode, setNeedsCode] = useState(false);
  const [setupKey, setSetupKey] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (new URLSearchParams(window.location.hash.slice(1)).has("invite")) {
      window.history.replaceState(null, "", window.location.pathname + window.location.search);
    }
    let current = true;
    void auth<Session>("session").then((data) => {
      if (current) setSession(data);
    }).catch((reason: unknown) => {
      if (current && (!(reason instanceof AuthError) || reason.status !== 401)) {
        setError("Не удалось проверить текущую сессию");
      }
    });
    return () => { current = false; };
  }, []);

  async function run(task: () => Promise<void>) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await task();
    } catch (reason) {
      if (reason instanceof AuthError) {
        if (reason.needsCode) setNeedsCode(true);
        if (reason.status === 401 && session) {
          setSession(null);
          setSetupKey("");
        }
        setError(reason.message);
      } else {
        setError("Не удалось связаться с сервером");
      }
    } finally {
      setBusy(false);
    }
  }

  function submit(event: FormEvent<HTMLFormElement>, task: (form: FormData) => Promise<void>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    void run(() => task(form));
  }

  async function redeem(form: FormData) {
    const password = field(form, "password");
    checkNewPassword(password);
    if (password !== field(form, "repeat")) {
      throw new AuthError("Введённые пароли различаются");
    }
    const data = await auth<{ login_id: string }>("redeem", { token: invite, password });
    setSession(null);
    setSetupKey("");
    setInvite(null);
    setLoginId(data.login_id);
    setNeedsCode(false);
    setNotice(`Пароль задан. Сохраните идентификатор для входа: ${data.login_id}.`);
  }

  async function login(form: FormData) {
    await auth("login", {
      login: loginId,
      password: field(form, "password"),
      code: field(form, "code") || null,
    });
    setSession(await auth<Session>("session"));
    setNeedsCode(false);
  }

  async function startTotp(active: Session) {
    const data = await auth<{ uri: string }>("totp/start", {}, active.csrf_token);
    setSetupKey(new URL(data.uri).searchParams.get("secret") ?? "");
  }

  async function confirmTotp(active: Session, form: FormData) {
    await auth("totp/confirm", { code: field(form, "code") }, active.csrf_token);
    setSetupKey("");
    setSession(await auth<Session>("session"));
    setNotice("Двухфакторная защита включена");
  }

  async function logout(active: Session) {
    await auth("logout", {}, active.csrf_token);
    setSession(null);
    setSetupKey("");
  }

  async function changePassword(active: Session, form: FormData) {
    const newPassword = field(form, "new_password");
    checkNewPassword(newPassword);
    if (newPassword !== field(form, "repeat_password")) {
      throw new AuthError("Введённые пароли различаются");
    }
    await auth("password/change", {
      current_password: field(form, "current_password"),
      new_password: newPassword,
    }, active.csrf_token);
    if (!loginId) setLoginId(active.employee_id);
    setSession(null);
    setSetupKey("");
    setNeedsCode(false);
    setNotice("Пароль изменён. Войдите с новым паролем.");
  }

  return (
    <section className="card">
      <h2>{invite ? "Задать пароль" : session ? "Учётная запись" : "Вход"}</h2>
      {error && <p className="error" role="alert">{error}</p>}
      {notice && <p role="status">{notice}</p>}
      {invite ? (
        <form onSubmit={(event) => submit(event, redeem)}>
          <p id="invite-password-hint">{PASSWORD_HINT}</p>
          <label>Новый пароль <input name="password" type="password" autoComplete="new-password" aria-describedby="invite-password-hint" required minLength={MIN_PASSWORD_LENGTH} maxLength={256} /></label>
          <label>Повторите пароль <input name="repeat" type="password" autoComplete="new-password" required minLength={MIN_PASSWORD_LENGTH} maxLength={256} /></label>
          <button disabled={busy}>Сохранить пароль</button>
        </form>
      ) : session ? (
        <>
          <p>{session.enrollment_only ? "Для входа настройте двухфакторную защиту." : `Идентификатор: ${session.employee_id}`}</p>
          {session.totp_enabled ? <p>Двухфакторная защита включена.</p> : setupKey ? (
            <form onSubmit={(event) => submit(event, (form) => confirmTotp(session, form))}>
              <p>Введите ключ в приложение-аутентификатор и укажите полученный код.</p>
              <label>Ключ настройки <input readOnly value={setupKey} /></label>
              <CodeInput />
              <button disabled={busy}>Подтвердить</button>
            </form>
          ) : (
            <button disabled={busy} onClick={() => void run(() => startTotp(session))}>
              Настроить двухфакторную защиту
            </button>
          )}
          {!session.enrollment_only && (
            <details>
              <summary>Сменить пароль</summary>
              <form onSubmit={(event) => submit(event, (form) => changePassword(session, form))}>
                <label>Текущий пароль <input name="current_password" type="password" autoComplete="current-password" required maxLength={256} /></label>
                <p id="change-password-hint">{PASSWORD_HINT}</p>
                <label>Новый пароль <input name="new_password" type="password" autoComplete="new-password" aria-describedby="change-password-hint" required minLength={MIN_PASSWORD_LENGTH} maxLength={256} /></label>
                <label>Повторите новый пароль <input name="repeat_password" type="password" autoComplete="new-password" required minLength={MIN_PASSWORD_LENGTH} maxLength={256} /></label>
                <button disabled={busy}>Сохранить новый пароль</button>
              </form>
            </details>
          )}
          {!session.enrollment_only && ["employee", "department_head"].includes(session.role) && (
            <>
              <MaterialPanel csrf={session.csrf_token} />
              <BuildPanel csrf={session.csrf_token} />
              <RagPanel csrf={session.csrf_token} />
              <PublicationPanel csrf={session.csrf_token} manager={session.role === "department_head"} />
            </>
          )}
          {!session.enrollment_only && session.role === "admin" && (
            <AdminPanel csrf={session.csrf_token} />
          )}
          {!session.enrollment_only && ["employee", "department_head", "hr"].includes(session.role) && (
            <HandoffPanel csrf={session.csrf_token} role={session.role} />
          )}
          <p><button disabled={busy} onClick={() => void run(() => logout(session))}>Выйти</button></p>
        </>
      ) : (
        <form onSubmit={(event) => submit(event, login)}>
          <label>Корпоративная почта или идентификатор <input name="login" value={loginId} onChange={(event) => setLoginId(event.target.value)} autoComplete="username" required maxLength={254} /></label>
          <label>Пароль <input name="password" type="password" autoComplete="current-password" required maxLength={256} /></label>
          {needsCode && <CodeInput />}
          <button disabled={busy}>Войти</button>
        </form>
      )}
    </section>
  );
}

function CodeInput() {
  return (
    <label>Код из приложения
      <input name="code" inputMode="numeric" pattern="[0-9]{6}" autoComplete="one-time-code" required />
    </label>
  );
}
