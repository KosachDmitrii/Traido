import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, CheckCircle2, RefreshCw, Search, XCircle } from "lucide-react";
import type { EvaluationResult, F3Diagnostics } from "@/lib/api";
import { fetchEvaluation, fetchF3Diagnostics } from "@/lib/api";
import { useDesk } from "@/context/DeskContext";
import { useT } from "@/i18n/I18nProvider";
import type { MessageKey } from "@/i18n";
import { TablePager, useTablePager, LoadingDots } from "@/ui";

function SymbolPicker({
  symbols,
  value,
  onChange,
}: {
  symbols: string[];
  value: string;
  onChange: (symbol: string) => void;
}) {
  const t = useT();
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
          placeholder={t("eval.picker.search")}
          aria-label={t("eval.picker.aria")}
          autoComplete="off"
          spellCheck={false}
        />
      </div>
      {open ? (
        <div className="eval-symbol-picker__menu" role="listbox" aria-label={t("eval.picker.az")}>
          {filtered.length === 0 ? (
            <div className="eval-symbol-picker__empty">{t("eval.picker.none")}</div>
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

const VERDICT_KEYS: Partial<Record<string, MessageKey>> = {
  PASS: "eval.verdict.PASS",
  FAIL_RETURN: "eval.verdict.FAIL_RETURN",
  FAIL_DRAWDOWN: "eval.verdict.FAIL_DRAWDOWN",
  FAIL_SHARPE: "eval.verdict.FAIL_SHARPE",
  FAIL_SAMPLE: "eval.verdict.FAIL_SAMPLE",
  NOT_EVALUATED: "eval.verdict.NOT_EVALUATED",
  INSUFFICIENT_SAMPLE: "eval.verdict.INSUFFICIENT_SAMPLE",
};

function verdictInfo(verdict: string, t: ReturnType<typeof useT>) {
  const key = VERDICT_KEYS[verdict];
  if (key) {
    const tone = verdict === "PASS" ? "pass" : verdict.startsWith("FAIL") ? "fail" : "warn";
    return { label: t(key), tone: tone as "pass" | "warn" | "fail" };
  }
  if (verdict.startsWith("INSUFFICIENT_SAMPLE")) {
    return { label: t("eval.verdict.INSUFFICIENT_SAMPLE"), tone: "warn" as const };
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

function chaseReasonLabel(t: ReturnType<typeof useT>, code: string): string {
  const key = `eval.f3.chase.${code}` as MessageKey;
  const translated = t(key);
  return translated !== key ? translated : code.replace(/_/g, " ").toLowerCase();
}

export function EvaluationPage() {
  const t = useT();
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
  const [f3Loading, setF3Loading] = useState(false);

  useEffect(() => {
    if (!symbol && sortedUniverse.length) setSymbol(sortedUniverse[0]);
  }, [symbol, sortedUniverse]);

  const loadF3 = useCallback(async () => {
    setF3Loading(true);
    try {
      setF3Error(null);
      setF3(await fetchF3Diagnostics());
    } catch (err) {
      setF3(null);
      setF3Error(err instanceof Error ? err.message : "f3_diagnostics_failed");
    } finally {
      setF3Loading(false);
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

  const verdict = data ? verdictInfo(data.verdict, t) : null;
  const fwd = f3?.forward_paper;
  const wait = f3?.wait_effectiveness;
  const sig = f3?.signal_quality;
  const tgt = f3?.target_quality;
  const regimeRows = data?.by_regime ?? [];
  const regimePager = useTablePager(regimeRows);

  return (
    <div className="eval-page">
      <section className="card">
        <div className="card-head">
          <div>
            <h2>{t("eval.f3.title")}</h2>
            <div className="sub">{t("eval.f3.sub")}</div>
          </div>
          <button
            type="button"
            className="eval-refresh"
            onClick={() => void loadF3()}
            disabled={f3Loading}
            aria-busy={f3Loading}
          >
            {f3Loading ? (
              <LoadingDots ariaLabel={t("common.loading")} />
            ) : (
              <RefreshCw size={15} strokeWidth={1.75} aria-hidden />
            )}
            {f3Loading ? t("eval.f3.loading") : t("eval.f3.refresh")}
          </button>
        </div>
        {f3Error ? <p className="empty-hint">{f3Error}</p> : null}
        {f3 && sig && wait && tgt && fwd ? (
          <>
            <div className="eval-grid">
              <Metric label={t("eval.f3.shadow")} value={String(sig.shadow_samples)} />
              <Metric
                label={t("eval.f3.avgQuality")}
                value={sig.avg_entry_quality != null ? String(sig.avg_entry_quality) : "—"}
              />
              <Metric
                label={t("eval.f3.oldWait")}
                value={String(wait.old_buy_to_wait)}
                hint={
                  wait.wait_rate_vs_old_buy_pct != null
                    ? t("eval.f3.oldWaitHint", { pct: wait.wait_rate_vs_old_buy_pct })
                    : undefined
                }
              />
              <Metric label={t("eval.f3.openWait")} value={String(wait.open_watches)} />
              <Metric
                label={t("eval.f3.mfe")}
                value={String(tgt.historical_mfe_samples.total ?? 0)}
                tone={tgt.reachability_ready ? "good" : "muted"}
                hint={
                  tgt.reachability_ready ? t("eval.f3.reachReady") : t("eval.f3.reachNeed")
                }
              />
              <Metric
                label={t("eval.f3.rth")}
                value={`${fwd.rth_shadow_samples}/${fwd.target_rth_samples}`}
                hint={t("eval.f3.rthHint", { pct: fwd.progress_pct })}
                tone={fwd.rth_shadow_samples >= fwd.target_rth_samples ? "good" : "muted"}
              />
            </div>
            {sig.top_chase_reasons.length ? (
              <>
                <p className="sub" style={{ marginTop: 12, marginBottom: 8 }}>
                  {t("eval.f3.chaseTitle")}
                </p>
                <ul className="eval-warnings">
                  {sig.top_chase_reasons.slice(0, 5).map(([code, n]) => (
                    <li key={code}>
                      {chaseReasonLabel(t, code)}: {n}
                    </li>
                  ))}
                </ul>
              </>
            ) : null}
            <p className="empty-hint">{t("eval.f3.forwardNote")}</p>
          </>
        ) : !f3Error ? (
          <p className="empty-hint">{t("eval.f3.loading")}</p>
        ) : null}
      </section>

      <section className="card">
        <div className="card-head">
          <div>
            <h2>{t("eval.strategy.title")}</h2>
            <div className="sub">{t("eval.strategy.sub")}</div>
          </div>
          <div className="eval-controls">
            <SymbolPicker symbols={sortedUniverse} value={symbol} onChange={setSymbol} />
            <button
              type="button"
              className="eval-refresh"
              onClick={() => void load(symbol, true)}
              disabled={loading || !symbol}
              aria-busy={loading}
            >
              {loading ? (
                <LoadingDots ariaLabel={t("common.loading")} />
              ) : (
                <RefreshCw size={15} strokeWidth={1.75} aria-hidden />
              )}
              {loading ? t("eval.strategy.running") : t("eval.strategy.recompute")}
            </button>
          </div>
        </div>

        {error ? <p className="empty-hint">{error}</p> : null}
        {!data && !error ? (
          <p className="empty-hint">
            {loading ? t("eval.strategy.empty.running") : t("eval.strategy.empty.pick")}
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
                  {t("eval.verdict.meta", {
                    sym: data.symbol,
                    name: data.name ? ` · ${data.name}` : "",
                    bars: data.bars,
                    n: data.oos_trade_count,
                  })}
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
                <h2>{t("eval.oos.title")}</h2>
                <div className="sub">{t("eval.oos.sub")}</div>
              </div>
            </div>
            <div className="eval-grid">
              <Metric
                label={t("eval.oos.return")}
                value={pct(data.oos_return_pct)}
                tone={data.oos_return_pct > 0 ? "good" : "bad"}
              />
              <Metric label={t("eval.oos.winRate")} value={pct(data.oos_win_rate * 100, 0)} />
              <Metric label={t("eval.oos.pf")} value={num(data.oos_profit_factor)} />
              <Metric label={t("eval.oos.dd")} value={pct(-data.oos_max_drawdown_pct)} tone="bad" />
              <Metric label={t("eval.oos.sharpe")} value={num(data.oos_sharpe)} />
              <Metric label={t("eval.oos.wfe")} value={num(data.walk_forward_efficiency)} />
            </div>
          </section>

          <section className="card">
            <div className="card-head">
              <div>
                <h2>{t("eval.bench.title", { bench: data.benchmark_symbol })}</h2>
                <div className="sub">{t("eval.bench.sub")}</div>
              </div>
            </div>
            <div className="eval-grid">
              <Metric
                label={t("eval.bench.strategy")}
                value={pct(data.oos_return_pct)}
                tone={data.beats_benchmark ? "good" : "muted"}
              />
              <Metric label={data.benchmark_symbol} value={pct(data.benchmark_return_pct)} />
              <Metric
                label={t("eval.bench.excess")}
                value={pct(data.excess_return_pct)}
                tone={data.beats_benchmark ? "good" : "bad"}
                hint={data.beats_benchmark ? t("eval.bench.beats") : t("eval.bench.loses")}
              />
              <Metric label={t("eval.bench.dd")} value={pct(-data.benchmark_max_drawdown_pct)} />
            </div>
          </section>

          <section className="card">
            <div className="card-head">
              <div>
                <h2>{t("eval.full.title")}</h2>
                <div className="sub">{t("eval.full.sub")}</div>
              </div>
            </div>
            <div className="eval-grid">
              <Metric label={t("eval.full.returnNet")} value={pct(data.return_pct)} />
              <Metric
                label={t("eval.full.returnGross")}
                value={pct(data.gross_return_pct)}
                hint={t("eval.full.beforeCosts")}
                tone="muted"
              />
              <Metric label={t("eval.full.costs")} value={money(data.total_costs)} tone="bad" />
              <Metric label={t("eval.full.trades")} value={String(data.trade_count)} />
              <Metric label={t("eval.full.expectancy")} value={`${num(data.expectancy_r)}R`} />
              <Metric label={t("eval.full.sharpe")} value={num(data.sharpe)} />
              <Metric label={t("eval.full.sortino")} value={num(data.sortino)} />
              <Metric label={t("eval.full.calmar")} value={num(data.calmar)} />
              <Metric label={t("eval.full.cagr")} value={pct(data.cagr_pct)} />
              <Metric label={t("eval.full.dd")} value={pct(-data.max_drawdown_pct)} />
            </div>
          </section>

          {data.by_regime.length ? (
            <section className="card">
              <div className="card-head">
                <div>
                  <h2>{t("eval.regime.title")}</h2>
                  <div className="sub">{t("eval.regime.sub")}</div>
                </div>
              </div>
              <table className="data-table">
                <thead>
                  <tr>
                    <th>{t("eval.regime.col.regime")}</th>
                    <th>{t("eval.regime.col.bars")}</th>
                    <th>{t("eval.regime.col.trades")}</th>
                    <th>{t("eval.regime.col.return")}</th>
                    <th>{t("eval.oos.winRate")}</th>
                    <th>{t("eval.oos.pf")}</th>
                    <th>{t("eval.regime.col.dd")}</th>
                  </tr>
                </thead>
                <tbody>
                  {regimePager.slice.map((r, i) => (
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
              <TablePager pager={regimePager} />
            </section>
          ) : null}
        </>
      ) : null}
    </div>
  );
}
