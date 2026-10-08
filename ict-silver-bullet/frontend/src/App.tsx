import {
  Component,
  lazy,
  Suspense,
  useEffect,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  age,
  ApiError,
  countdown,
  dashboard,
  number,
  request,
  time,
  type Session,
} from "./api";
import type { DashboardSnapshot } from "./types";
const EquityChart = lazy(() =>
  import("./Charts").then((m) => ({ default: m.EquityChart })),
);
const SetupChart = lazy(() =>
  import("./Charts").then((m) => ({ default: m.SetupChart })),
);
class ChartBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    return this.state.failed ? (
      <div className="empty">
        Chart unavailable.{" "}
        <button onClick={() => window.location.reload()}>Reload page</button>
      </div>
    ) : (
      this.props.children
    );
  }
}
type AnyPanel = {
  status: string;
  updated_at?: string | null;
  message?: string | null;
  data?: unknown;
};

function Brand() {
  return (
    <div className="brand">
      <span className="brand-mark">↗</span>
      <span>
        ICT<span className="brand-sub">SILVER BULLET</span>
      </span>
    </div>
  );
}
function Login({
  onLogin,
  message,
}: {
  onLogin: (s: Session) => void;
  message?: string;
}) {
  const [error, setError] = useState(message || "");
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    const fields = new FormData(event.currentTarget);
    try {
      onLogin(
        await request<Session>("/auth/login", {
          method: "POST",
          body: JSON.stringify({
            username: fields.get("username"),
            password: fields.get("password"),
          }),
        }),
      );
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 401
          ? "Invalid username or password."
          : e instanceof Error
            ? e.message
            : "Unable to sign in.",
      );
    } finally {
      setBusy(false);
    }
  }
  return (
    <main className="login-page">
      <div className="login-card">
        <Brand />
        <span className="eyebrow">YOUR TRADING WORKSPACE</span>
        <h1>Welcome back.</h1>
        <p>Sign in to monitor your account, risk and setups.</p>
        <form onSubmit={submit}>
          <label>
            Username
            <input
              name="username"
              autoComplete="username"
              required
              maxLength={256}
            />
          </label>
          <label>
            Password
            <input
              name="password"
              type="password"
              autoComplete="current-password"
              required
              maxLength={1024}
            />
          </label>
          {error && (
            <p role="alert" className="error">
              {error}
            </p>
          )}
          <button className="primary" disabled={busy}>
            {busy ? "Signing in…" : "Sign in →"}
          </button>
        </form>
        <p className="footnote">
          Private dashboard · Sessions expire after 8 hours
        </p>
      </div>
    </main>
  );
}
function State({ panel, now }: { panel: AnyPanel; now: number }) {
  const seconds = age(panel.updated_at, now);
  const status =
    seconds !== null && seconds > 30 && panel.status === "ready"
      ? "stale"
      : panel.status;
  return (
    <span
      className={`panel-state ${status}`}
      title={panel.message || undefined}
    >
      {status}
      {seconds !== null && ` · ${seconds}s ago`}
    </span>
  );
}
function Card({
  title,
  subtitle,
  panel,
  now,
  children,
  className = "",
  id,
}: {
  title: string;
  subtitle?: string;
  panel?: AnyPanel;
  now: number;
  children: ReactNode;
  className?: string;
  id?: string;
}) {
  return (
    <section className={`card ${className}`} id={id}>
      <div className="card-head">
        <div>
          <h2>{title}</h2>
          {subtitle && <p>{subtitle}</p>}
        </div>
        {panel && <State panel={panel} now={now} />}
      </div>
      {panel &&
        (panel.status === "stale" || panel.status === "unavailable") && (
          <p className="panel-warning">
            {panel.message || "Data unavailable. Retrying."}
          </p>
        )}
      {children}
    </section>
  );
}
function Empty({ children = "No data yet" }: { children?: ReactNode }) {
  return <div className="empty">{children}</div>;
}
function Gauge({
  gauge,
  label,
  fresh,
}: {
  gauge: NonNullable<DashboardSnapshot["risk"]["data"]>["daily"] | undefined;
  label: string;
  fresh: boolean;
}) {
  const value = gauge?.value;
  const severity =
    value != null && gauge && fresh
      ? value >= gauge.hard
        ? "danger"
        : value >= gauge.soft
          ? "warning"
          : "safe"
      : "unknown";
  return (
    <>
      <div className="metric">
        <strong>{value == null ? "—" : number(value * 100) + "%"}</strong>
        <span className={`tag ${severity}`}>
          {severity === "safe"
            ? "Within limits"
            : severity === "danger"
              ? "Hard threshold"
              : severity === "warning"
                ? "Soft threshold"
                : value != null
                  ? "Last reported"
                  : "Unavailable"}
        </span>
      </div>
      <div
        className="risk-track"
        role="meter"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={
          gauge ? Math.max(gauge.limit * 100, (value || 0) * 100) : 100
        }
        aria-valuenow={value == null ? undefined : Math.max(0, value * 100)}
        aria-valuetext={
          value == null
            ? "Unavailable"
            : `${number(value * 100)} percent drawdown`
        }
      >
        <div
          className={`risk-fill ${severity}`}
          style={{
            width: gauge
              ? `${Math.min(100, Math.max(0, ((value || 0) / gauge.limit) * 100))}%`
              : "0%",
          }}
        />
        {gauge && (
          <>
            <i style={{ left: `${(gauge.soft / gauge.limit) * 100}%` }} />
            <i style={{ left: `${(gauge.hard / gauge.limit) * 100}%` }} />
          </>
        )}
      </div>
      <div className="risk-labels">
        <span>Soft {gauge ? number(gauge.soft * 100) : "—"}%</span>
        <span>Hard {gauge ? number(gauge.hard * 100) : "—"}%</span>
        <span>Limit {gauge ? number(gauge.limit * 100) : "—"}%</span>
      </div>
    </>
  );
}
function Trades({
  trades,
}: {
  trades: NonNullable<DashboardSnapshot["trades"]["data"]>;
}) {
  const [filter, setFilter] = useState(""),
    [sort, setSort] = useState<"time" | "rr">("time");
  const rows = trades
    .filter((t) =>
      `${t.pair} ${t.direction} ${t.status}`
        .toLowerCase()
        .includes(filter.toLowerCase()),
    )
    .sort((a, b) =>
      sort === "rr"
        ? (b.rr ?? -Infinity) - (a.rr ?? -Infinity)
        : Date.parse(b.timestamp || "") - Date.parse(a.timestamp || ""),
    );
  return (
    <>
      <div className="table-tools">
        <input
          aria-label="Filter trades"
          placeholder="Filter pair, direction or status…"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
        <select
          aria-label="Sort trades"
          value={sort}
          onChange={(e) => setSort(e.target.value as "time" | "rr")}
        >
          <option value="time">Newest first</option>
          <option value="rr">Highest R:R</option>
        </select>
      </div>
      {rows.length ? (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                {[
                  "Opened · UTC",
                  "Pair",
                  "Direction",
                  "Entry",
                  "Stop loss",
                  "Take profit",
                  "Score",
                  "R:R",
                  "Status",
                  "Realized R",
                ].map((h) => (
                  <th key={h}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((t, i) => (
                <tr key={t.id ?? i}>
                  <td>{time(t.timestamp)}</td>
                  <td className="pair">{t.pair || "—"}</td>
                  <td>{t.direction || "—"}</td>
                  <td>{number(t.entry, 5)}</td>
                  <td>{number(t.stop_loss, 5)}</td>
                  <td>{number(t.take_profit, 5)}</td>
                  <td>{number(t.confluence_score, 0)}</td>
                  <td>{number(t.rr)}</td>
                  <td>
                    <span className="tag neutral">{t.status || "—"}</span>
                  </td>
                  <td className={(t.realized_r ?? 0) > 0 ? "positive" : ""}>
                    {number(t.realized_r)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <Empty>
          {filter ? "No trades match this filter" : "No trades yet"}
        </Empty>
      )}
    </>
  );
}
function Workspace({
  session,
  onLogout,
}: {
  session: Session;
  onLogout: (message?: string) => void;
}) {
  const client = useQueryClient();
  const query = useQuery({
    queryKey: ["dashboard"],
    queryFn: ({ signal }) => dashboard(signal),
    refetchInterval: 10000,
    refetchIntervalInBackground: true,
  });
  const [clock, setClock] = useState(performance.now()),
    [logoutError, setLogoutError] = useState(""),
    [loggingOut, setLoggingOut] = useState(false);
  useEffect(() => {
    const timer = setInterval(() => setClock(performance.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  const [anchor, setAnchor] = useState({
    server: Date.now(),
    client: performance.now(),
  });
  useEffect(() => {
    if (query.data)
      setAnchor({
        server: Date.parse(query.data.generated_at),
        client: performance.now(),
      });
  }, [query.data]);
  useEffect(() => {
    if (query.error instanceof ApiError && query.error.status === 401) {
      void client.cancelQueries();
      client.removeQueries({ queryKey: ["dashboard"] });
      onLogout(query.error.message);
    }
  }, [query.error, client, onLogout]);
  async function logout() {
    setLoggingOut(true);
    try {
      await request("/auth/logout", { method: "POST" });
      await client.cancelQueries();
      client.clear();
      onLogout();
    } catch {
      setLogoutError("Sign out failed. Please retry.");
    } finally {
      setLoggingOut(false);
    }
  }
  const now = anchor.server + clock - anchor.client,
    data = query.data;
  const equity = data?.equity.data,
    risk = data?.risk.data,
    setup = data?.setup.data;
  const newsAge = age(data?.news.updated_at, now);
  const riskAge = age(data?.risk.updated_at, now);
  const riskFresh =
    !query.error &&
    data?.risk.status === "ready" &&
    riskAge !== null &&
    riskAge <= 30;
  const newsFresh =
    !query.error &&
    data?.news.status === "ready" &&
    newsAge !== null &&
    newsAge <= 30;
  const kz = data?.killzone,
    active =
      kz?.closes_at && now < Date.parse(kz.closes_at) && kz.state === "armed";
  return (
    <div className="shell">
      <aside className="sidebar">
        <Brand />
        <nav aria-label="Dashboard sections">
          <a href="#overview" className="selected">
            ◈ <span>Overview</span>
          </a>
          <a href="#equity">
            ⌁ <span>Equity & risk</span>
          </a>
          <a href="#setup">
            ◇ <span>Latest setup</span>
          </a>
          <a href="#trades">
            ≡ <span>Trade history</span>
          </a>
        </nav>
        <div className="sidebar-bottom">
          <span className="eyebrow">EXECUTION WORKSPACE</span>
          <p>
            New York sessions
            <br />
            All timestamps in UTC
          </p>
          <span className="tag neutral">Read only</span>
        </div>
      </aside>
      <main className="workspace" id="overview">
        <header className="topbar">
          <div>
            <span className="eyebrow">ACCOUNT MONITOR</span>
            <h1>
              Trading overview<span className="title-dot">.</span>
            </h1>
            <p>Every setup. Every limit. One view.</p>
          </div>
          <div className="user-actions">
            <span className={`connection ${query.error ? "warning" : ""}`}>
              <i />
              {query.error ? "Connection lost" : "Polling · 10s"}
            </span>
            <span className="username">{session.username}</span>
            <button onClick={logout} disabled={loggingOut}>
              {loggingOut ? "Signing out…" : "Sign out"}
            </button>
          </div>
        </header>
        {logoutError && (
          <p role="alert" className="error">
            {logoutError}
          </p>
        )}
        {query.error && (
          <div className="banner" role="alert">
            {query.error.message}{" "}
            {data ? "Showing the last received snapshot." : ""}{" "}
            <button onClick={() => void query.refetch()}>Retry now</button>
          </div>
        )}
        {!data && (
          <Empty>
            {query.isPending
              ? "Loading your workspace…"
              : "Waiting for dashboard data."}
          </Empty>
        )}
        <div className="summary-grid">
          <Card title="Account equity" panel={data?.equity} now={now}>
            <div className="metric">
              <strong>
                {equity?.latest ? "$" + number(equity.latest.equity) : "—"}
              </strong>
              <span className="tag neutral">USD</span>
            </div>
            <p className="muted">
              Balance{" "}
              {equity?.latest?.balance != null
                ? "$" + number(equity.latest.balance)
                : "unavailable"}
            </p>
            <p className="footnote">Snapshot {time(equity?.latest?.ts)}</p>
          </Card>
          <Card title="Daily drawdown" panel={data?.risk} now={now}>
            <Gauge
              gauge={risk?.daily}
              label="Daily drawdown"
              fresh={riskFresh}
            />
          </Card>
          <Card title="Overall drawdown" panel={data?.risk} now={now}>
            <Gauge
              gauge={risk?.overall}
              label="Overall drawdown"
              fresh={riskFresh}
            />
          </Card>
          <Card title="Killzone clock" now={now} className="killzone">
            <span className={`tag ${active ? "safe" : "neutral"}`}>
              {active ? "Armed" : "Idle"}
            </span>
            <div className="countdown">
              {kz ? countdown(active ? kz.closes_at! : kz.opens_at, now) : "—"}
            </div>
            <p className="muted">
              {kz
                ? `${active ? kz.active_window : kz.next_window} · ${active ? "closes" : "opens"} ${time(active ? kz.closes_at : kz.opens_at)}`
                : "Waiting for server time"}
            </p>
            <p className="footnote">
              Window status; market and risk guards apply
            </p>
          </Card>
        </div>
        <div className="main-grid">
          <Card
            id="equity"
            title="Equity curve"
            subtitle="Trailing 24 hours · persisted drawdown baselines"
            panel={data?.equity}
            now={now}
          >
            <ChartBoundary>
              <Suspense fallback={<Empty>Loading chart…</Empty>}>
                {equity?.points.length ? (
                  <EquityChart equity={equity} />
                ) : (
                  <Empty>No equity history yet</Empty>
                )}
              </Suspense>
            </ChartBoundary>
          </Card>
          <Card
            title="Economic calendar"
            subtitle="High impact · next 24 hours & recent 15 minutes"
            panel={data?.news}
            now={now}
          >
            <div className="blackouts">
              {data?.news.data?.blackout.map((b) => (
                <span
                  className={`tag ${newsFresh ? (b.blocked ? "warning" : "safe") : "unknown"}`}
                  key={b.pair}
                >
                  {b.pair} ·{" "}
                  {newsFresh ? (b.blocked ? "blackout" : "clear") : "unknown"}
                </span>
              ))}
            </div>
            {data?.news.data?.events.length ? (
              <ul className="news-list">
                {data.news.data.events.map((e, i) => (
                  <li key={`${e.scheduled_at}-${i}`}>
                    <span className="news-currency">{e.currency}</span>
                    <div>
                      <strong>{e.title}</strong>
                      <span>{time(e.scheduled_at)}</span>
                    </div>
                    <span className="news-impact">HIGH</span>
                  </li>
                ))}
              </ul>
            ) : (
              <Empty>
                {data?.news.status === "ready"
                  ? "No high impact events in this window"
                  : "Calendar unavailable"}
              </Empty>
            )}
            <p className="footnote">
              Blackout: ±15 minutes per currency. Stale data requires review.
            </p>
          </Card>
        </div>
        <div className="main-grid">
          <Card
            id="setup"
            title="Latest setup"
            subtitle={
              setup
                ? `${setup.pair} · ${setup.direction || "—"} · ${time(setup.detected_at)}`
                : "Latest detected signal"
            }
            panel={data?.setup}
            now={now}
          >
            <ChartBoundary>
              <Suspense fallback={<Empty>Loading chart…</Empty>}>
                {setup?.candles.length ? (
                  <SetupChart setup={setup} />
                ) : (
                  <Empty>No setup candles yet</Empty>
                )}
              </Suspense>
            </ChartBoundary>
            {setup && (
              <p className="footnote">
                {setup.source === "recorded"
                  ? "Recorded setup candles"
                  : "Historical M1 context at detection"}
                {setup.missing_evidence.length
                  ? ` · Evidence not stored: ${setup.missing_evidence.join(", ")}`
                  : ""}
              </p>
            )}
          </Card>
          <Card
            title="Open positions"
            subtitle="Reported positions · floating P/L when available"
            panel={data?.positions}
            now={now}
          >
            {data?.positions.data?.length ? (
              <div className="position-list">
                {data.positions.data.map((p, i) => (
                  <div className="position" key={p.id ?? i}>
                    <div>
                      <strong>{p.pair || "—"}</strong>
                      <span>
                        {p.direction} · leg {p.leg || "—"} · {number(p.lots)}{" "}
                        lots
                      </span>
                    </div>
                    <div>
                      <strong>{number(p.floating_pnl)}</strong>
                      <span>Floating P/L</span>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <Empty>
                {data?.positions.status === "empty"
                  ? "No open positions"
                  : "Positions unavailable"}
              </Empty>
            )}
            <p className="footnote">
              Floating P/L is unavailable until reported by the data source.
            </p>
          </Card>
        </div>
        <Card
          id="trades"
          title="Trade history"
          subtitle="Latest 100 trades · actual fills & realized R"
          panel={data?.trades}
          now={now}
        >
          {data?.trades.data ? (
            <Trades trades={data.trades.data} />
          ) : (
            <Empty>Trade history unavailable</Empty>
          )}
        </Card>
        <footer>
          ICT Silver Bullet{" "}
          <span>Server snapshot {time(data?.generated_at)} · API v1</span>
        </footer>
      </main>
    </div>
  );
}
export default function App() {
  const [session, setSession] = useState<Session | null | undefined>(undefined),
    [message, setMessage] = useState(""),
    [sessionError, setSessionError] = useState("");
  const client = useQueryClient();
  function checkSession() {
    setSessionError("");
    void request<Session>("/auth/session")
      .then(setSession)
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) setSession(null);
        else setSessionError("Unable to check your session. Please retry.");
      });
  }
  useEffect(() => {
    let mounted = true;
    void request<Session>("/auth/session")
      .then((s) => {
        if (mounted) setSession(s);
      })
      .catch((e) => {
        if (mounted) {
          if (e instanceof ApiError && e.status === 401) setSession(null);
          else setSessionError("Unable to check your session. Please retry.");
        }
      });
    return () => {
      mounted = false;
    };
  }, []);
  if (session === undefined)
    return (
      <main className="login-page">
        <div className="login-card">
          <Brand />
          <p role="status">{sessionError || "Checking your session…"}</p>
          {sessionError && <button onClick={checkSession}>Retry</button>}
        </div>
      </main>
    );
  return session ? (
    <Workspace
      session={session}
      onLogout={(m) => {
        client.clear();
        setMessage(m || "");
        setSession(null);
      }}
    />
  ) : (
    <Login message={message} onLogin={setSession} />
  );
}
