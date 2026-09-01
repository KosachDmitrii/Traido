import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, CheckCircle2, RefreshCw, Search, XCircle } from "lucide-react";
import type { EvaluationResult, F3Diagnostics } from "@/lib/api";
import { fetchEvaluation, fetchF3Diagnostics } from "@/lib/api";
import { useDesk } from "@/context/DeskContext";

function SymbolPicker({
  symbols,
  value,
  onChange,
}: {
  symbols: string[];
  value: string;
  onChange: (symbol: string) => void;
}) {
  const rootRef = useRef<HTMLDivElement>(null);
  const [query, setQuery] = useState(value);
  const [open, setOpen] = useState(false);

  const sorted = useMemo(
    () => [...new Set(symbols.map((s) => s.toUpperCase()))].sort((a, b) => a.localeCompare(b)),
    [symbols],
  );

  const filtered = useMemo(() => {
    const q = query.trim().toUpperCase();
    if (!q) return sorted;
    return sorted.filter((s) => s.startsWith(q) || s.includes(q));
  }, [query, sorted]);

  useEffect(() => {
    setQuery(value);
  }, [value]);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  const pick = (sym: string) => {
    onChange(sym);
    setQuery(sym);
    setOpen(false);
  };

  const commitTyped = () => {
    const q = query.trim().toUpperCase();
    if (!q) return;
    const exact = sorted.find((s) => s === q);
    if (exact) {
      pick(exact);
      return;
    }
    if (filtered[0]) pick(filtered[0]);
  };

  return (
    <div className="eval-symbol-picker" ref={rootRef}>
      <div className="eval-symbol-picker__field">
        <Search size={15} strokeWidth={1.75} aria-hidden />
        <input
          className="logs-search eval-symbol-picker__input"
          value={query}
          onChange={(e) => {
            setQuery(e.target.value.toUpperCase());
            setOpen(true);
          }}
          onFocus={() => setOpen(true)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              commitTyped();
            }
            if (e.key === "Escape") setOpen(false);
          }}
          placeholder="Search symbol…"
          aria-label="Search symbol"
          autoComplete="off"
          spellCheck={false}
        />
      </div>
      {open ? (
        <div className="eval-symbol-picker__menu" role="listbox" aria-label="Symbols A–Z">
          {filtered.length === 0 ? (
            <div className="eval-symbol-picker__empty">No match</div>
          ) : (
            filtered.map((s) => (
              <button
                key={s}
                type="button"
                role="option"
                aria-selected={s === value}
                className={`eval-symbol-picker__option mono${s === value ? " is-active" : ""}`}
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => pick(s)}
              >
                {s}
              </button>
            ))
          )}
        </div>
      ) : null}
    </div>
  );
}

/**
 * Evaluation page.
 *
 * Deliberately leads with the out-of-sample numbers. The full-sample backtest
 * is shown underneath and greyed in importance, because it is the number most
 * likely to be flattering and least likely to be true.
 *
 * Above that: F3 Paper diagnostics (signal / WAIT / target / forward progress).
 */

const VERDICT_COPY: Record<string, { label: string; tone: "pass" | "warn" | "fail" }> = {
  PASS: { label: "Edge survives out of sample", tone: "pass" },
  FAIL_NEGATIVE_OOS: { label: "Loses money out of sample", tone: "fail" },
  FAIL_WEAK_PROFIT_FACTOR: { label: "Profit factor too thin to trade", tone: "fail" },
  FAIL_OVERFIT: { label: "Overfit — parameters do not generalise", tone: "fail" },
  NOT_EVALUATED: { label: "Not enough history to walk forward", tone: "warn" },
};

function verdictInfo(verdict: string) {
  if (VERDICT_COPY[verdict]) return VERDICT_COPY[verdict];
  if (verdict.startsWith("INSUFFICIENT_SAMPLE")) {
    return { label: "Too few out-of-sample trades to judge", tone: "warn" as const };
  }
  return { label: verdict, tone: "warn" as const };
}

function pct(v: number | null | undefined, digits = 1): string {
  if (v == null || Number.isNaN(v)) return "—";
  return `${v >= 0 ? "+" : ""}${v.toFixed(digits)}%`;
}

function num(v: number | null | undefined, digits = 2): string {
  if (v == null || Number.isNaN(v)) return "—";
  return v.toFixed(digits);
}

function money(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "—";
  return `$${Math.round(v).toLocaleString()}`;
}

function Metric({
  label,
  value,
  hint,
  tone,
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: "good" | "bad" | "muted";
}) {
  return (
    <div className={`eval-metric${tone ? ` eval-metric--${tone}` : ""}`}>
      <span className="eval-metric__label">{label}</span>
      <strong className="eval-metric__value mono">{value}</strong>
      {hint ? <span className="eval-metric__hint">{hint}</span> : null}
    </div>
  );
}

export function EvaluationPage() {
  const { desk } = useDesk();
  const universe = desk?.scanner?.universe ?? [];
  const sortedUniverse = useMemo(
    () => [...new Set(universe.map((s) => s.toUpperCase()))].sort((a, b) => a.localeCompare(b)),
    [universe],
  );
  const [symbol, setSymbol] = useState<string>("");
  const [data, setData] = useState<EvaluationResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [f3, setF3] = useState<F3Diagnostics | null>(null);
  const [f3Error, setF3Error] = useState<string | null>(null);

  useEffect(() => {
    if (!symbol && sortedUniverse.length) setSymbol(sortedUniverse[0]);
  }, [symbol, sortedUniverse]);

  const loadF3 = useCallback(async () => {
    try {
      setF3Error(null);
      setF3(await fetchF3Diagnostics());
    } catch (err) {
      setF3(null);
      setF3Error(err instanceof Error ? err.message : "f3_diagnostics_failed");
    }
  }, []);

  const load = useCallback(async (target: string, refresh = false) => {
    if (!target) return;
    setLoading(true);
    setError(null);
    try {
      setData(await fetchEvaluation(target, refresh));
    } catch (err) {
      setData(null);
      setError(err instanceof Error ? err.message : "evaluation_failed");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (symbol) void load(symbol);
  }, [symbol, load]);

  useEffect(() => {
    void loadF3();
    const id = window.setInterval(() => void loadF3(), 60_000);
    return () => window.clearInterval(id);
  }, [loadF3]);

  const verdict = data ? verdictInfo(data.verdict) : null;
  const fwd = f3?.forward_paper;
  const wait = f3?.wait_effectiveness;
  const sig = f3?.signal_quality;
  const tgt = f3?.target_quality;

  return (
    <div className="eval-page">
      <section className="card">
        <div className="card-head">
          <div>
            <h2>F3 entry diagnostics</h2>
            <div className="sub">
              Signal quality · WAIT effectiveness · target reachability · forward Paper
            </div>
          </div>
          <button type="button" className="eval-refresh" onClick={() => void loadF3()}>
            <RefreshCw size={15} strokeWidth={1.75} aria-hidden />
            Refresh
          </button>
        </div>
        {f3Error ? <p className="empty-hint">{f3Error}</p> : null}
        {f3 && sig && wait && tgt && fwd ? (
          <>
            <div className="eval-grid">
              <Metric label="Shadow samples" value={String(sig.shadow_samples)} />
              <Metric
                label="Avg entry quality"
                value={sig.avg_entry_quality != null ? String(sig.avg_entry_quality) : "—"}
              />
              <Metric
                label="Old BUY → WAIT"
                value={String(wait.old_buy_to_wait)}
                hint={
                  wait.wait_rate_vs_old_buy_pct != null
                    ? `${wait.wait_rate_vs_old_buy_pct}% of old buys`
                    : undefined
                }
              />
              <Metric label="Open WAIT watches" value={String(wait.open_watches)} />
              <Metric
                label="Historical MFE n"
                value={String(tgt.historical_mfe_samples.total ?? 0)}
                tone={tgt.reachability_ready ? "good" : "muted"}
                hint={tgt.reachability_ready ? "Reachability armed (≥30)" : "Need ≥30 samples"}
              />
              <Metric
                label="RTH forward progress"
                value={`${fwd.rth_shadow_samples}/${fwd.target_rth_samples}`}
                hint={`${fwd.progress_pct}% — do not claim FIXED yet`}
                tone={fwd.rth_shadow_samples >= fwd.target_rth_samples ? "good" : "muted"}
              />
            </div>
            {sig.top_chase_reasons.length ? (
              <ul className="eval-warnings">
                {sig.top_chase_reasons.slice(0, 5).map(([code, n]) => (
                  <li key={code}>
                    {code}: {n}
                  </li>
                ))}
              </ul>
            ) : null}
            <p className="empty-hint">{fwd.note}</p>
          </>
        ) : !f3Error ? (
          <p className="empty-hint">Loading F3 diagnostics…</p>
        ) : null}
      </section>

      <section className="card">
        <div className="card-head">
          <div>
            <h2>Strategy evaluation</h2>
            <div className="sub">
              Out-of-sample results after commission, spread and slippage
            </div>
          </div>
          <div className="eval-controls">
            <SymbolPicker symbols={sortedUniverse} value={symbol} onChange={setSymbol} />
            <button
              type="button"
              className="eval-refresh"
              onClick={() => void load(symbol, true)}
              disabled={loading || !symbol}
            >
              <RefreshCw size={15} strokeWidth={1.75} aria-hidden />
              {loading ? "Running…" : "Recompute"}
            </button>
          </div>
        </div>

        {error ? <p className="empty-hint">{error}</p> : null}
        {!data && !error ? (
          <p className="empty-hint">
            {loading ? "Running walk-forward evaluation…" : "Pick a symbol to evaluate."}
          </p>
        ) : null}

        {data && verdict ? (
          <>
            <div className={`eval-verdict eval-verdict--${verdict.tone}`}>
              {verdict.tone === "pass" ? (
                <CheckCircle2 size={20} strokeWidth={1.75} aria-hidden />
              ) : verdict.tone === "fail" ? (
                <XCircle size={20} strokeWidth={1.75} aria-hidden />
              ) : (
                <AlertTriangle size={20} strokeWidth={1.75} aria-hidden />
              )}
              <div>
                <strong>{verdict.label}</strong>
                <span className="sub">
                  {data.symbol} · {data.bars} bars · {data.oos_trade_count} out-of-sample trades
                </span>
              </div>
            </div>

            {data.warnings.length ? (
              <ul className="eval-warnings">
                {data.warnings.map((w) => (
                  <li key={w}>{w}</li>
                ))}
              </ul>
            ) : null}
          </>
        ) : null}
      </section>

      {data ? (
        <>
          <section className="card">
            <div className="card-head">
              <div>
                <h2>Out of sample</h2>
                <div className="sub">Data the parameters were never fitted on</div>
              </div>
            </div>
            <div className="eval-grid">
              <Metric
                label="Return"
                value={pct(data.oos_return_pct)}
                tone={data.oos_return_pct > 0 ? "good" : "bad"}
              />
              <Metric label="Win rate" value={pct(data.oos_win_rate * 100, 0)} />
              <Metric label="Profit factor" value={num(data.oos_profit_factor)} />
              <Metric label="Max drawdown" value={pct(-data.oos_max_drawdown_pct)} tone="bad" />
              <Metric label="Sharpe" value={num(data.oos_sharpe)} />
              <Metric
                label="Walk-forward efficiency"
                value={num(data.walk_forward_efficiency)}
                hint="Below 0.5 means the fit is noise"
              />
            </div>
          </section>

          <section className="card">
            <div className="card-head">
              <div>
                <h2>Versus {data.benchmark_symbol}</h2>
                <div className="sub">Buy and hold over the same bars, same costs</div>
              </div>
            </div>
            <div className="eval-grid">
              <Metric
                label="Strategy"
                value={pct(data.oos_return_pct)}
                tone={data.beats_benchmark ? "good" : "muted"}
              />
              <Metric label={data.benchmark_symbol} value={pct(data.benchmark_return_pct)} />
              <Metric
                label="Excess return"
                value={pct(data.excess_return_pct)}
                tone={data.beats_benchmark ? "good" : "bad"}
                hint={data.beats_benchmark ? "Beats the index" : "Index wins — no reason to trade"}
              />
              <Metric label="Benchmark drawdown" value={pct(-data.benchmark_max_drawdown_pct)} />
            </div>
          </section>

          <section className="card">
            <div className="card-head">
              <div>
                <h2>Full sample</h2>
                <div className="sub">In-sample fit — the optimistic number</div>
              </div>
            </div>
            <div className="eval-grid">
              <Metric label="Return (net)" value={pct(data.return_pct)} />
              <Metric
                label="Return (gross)"
                value={pct(data.gross_return_pct)}
                hint="Before costs"
                tone="muted"
              />
              <Metric label="Costs paid" value={money(data.total_costs)} tone="bad" />
              <Metric label="Trades" value={String(data.trade_count)} />
              <Metric label="Expectancy" value={`${num(data.expectancy_r)}R`} />
              <Metric label="Sharpe" value={num(data.sharpe)} />
              <Metric label="Sortino" value={num(data.sortino)} />
              <Metric label="Calmar" value={num(data.calmar)} />
              <Metric label="CAGR" value={pct(data.cagr_pct)} />
              <Metric label="Max drawdown" value={pct(-data.max_drawdown_pct)} />
            </div>
          </section>

          {data.by_regime.length ? (
            <section className="card">
              <div className="card-head">
                <div>
                  <h2>By market regime</h2>
                  <div className="sub">
                    A strategy that only works in one regime is a bet on that regime
                  </div>
                </div>
              </div>
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Regime</th>
                    <th>Bars</th>
                    <th>Trades</th>
                    <th>Return</th>
                    <th>Win rate</th>
                    <th>Profit factor</th>
                    <th>Max DD</th>
                  </tr>
                </thead>
                <tbody>
                  {data.by_regime.map((r, i) => (
                    <tr key={`${r.regime}-${i}`}>
                      <td>{r.regime.replace(/_/g, " ")}</td>
                      <td className="mono">{r.bars}</td>
                      <td className="mono">{r.trade_count}</td>
                      <td className="mono">{pct(r.return_pct)}</td>
                      <td className="mono">{pct(r.win_rate * 100, 0)}</td>
                      <td className="mono">{num(r.profit_factor)}</td>
                      <td className="mono">{pct(-r.max_drawdown_pct)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          ) : null}
        </>
      ) : null}
    </div>
  );
}
