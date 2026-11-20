import AuthPanel from "./AuthPanel";
import SourcePage, { sourceRoute } from "./SourcePage";

export default function App() {
  const path = window.location.pathname;
  const source = sourceRoute(path);
  return (
    <main className="page">
      <header className="app-header">
        <h1><a href="/">Эстафета</a></h1>
      </header>
      {source ? <SourcePage route={source} /> : path === "/" ? <AuthPanel /> : (
        <section className="card">
          <h2>Страница не найдена</h2>
          <a href="/">На главную</a>
        </section>
      )}
    </main>
  );
}
