import { useDesk } from "@/context/DeskContext";

/**
 * Why only a handful of proposals appeared.
 *
 * The desk shows two cards and the operator's question is always the same one:
 * did it look at anything? Before this, the only available answer was the
 * number of proposals, which is the one number that cannot distinguish "the
 * market was quiet" from "the provider was down" from "the queue was full".
 *
 * So this is the funnel as the backend keeps it — every stage, with the count
 * that entered it — plus what the cycle cost and when the next one is due.
 */

type Row = { label: string; value: number; note?: string; dim?: boolean };

function seconds(value: number | undefined): string {
  if (value == null) return "—";
  if (value < 1) return `${Math.round(value * 1000)}ms`;
  return `${value.toFixed(value < 10 ? 1 : 0)}s`;
}

function nextScan(secondsUntil: number | undefined): string {
  if (secondsUntil == null) return "—";
  if (secondsUntil <= 0) return "due";
  const at = new Date(Date.now() + secondsUntil * 1000);
  return at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function ScanFunnelCard() {
  const { desk } = useDesk();
  const scanner = desk?.scanner;
  const funnel = scanner?.funnel;

  if (!funnel || !funnel.universe_total) {
    return (
      <section className="card scan-funnel">
        <div className="card-head">
          <div>
            <h2>Scan funnel</h2>
            <div className="sub">No completed cycle yet</div>
          </div>
        </div>
      </section>
    );
  }

  const rows: Row[] = [
    { label: "Universe", value: funnel.universe_total },
    { label: "Eligible", value: funnel.structurally_eligible, note: "structural" },
    { label: "Market passed", value: funnel.market_filter_passed, note: "liquidity" },
    { label: "Quant shortlisted", value: funnel.quant_shortlisted, note: "top-K" },
    { label: "Deep analysed", value: funnel.deep_analysis_started, note: "expensive" },
    { label: "Risk passed", value: funnel.risk_passed },
    { label: "Published", value: funnel.published },
    { label: "Outranked", value: funnel.final_outranked, dim: true },
  ];

  const problems: Row[] = [
    { label: "Provider failed", value: funnel.provider_failed },
    { label: "Stale data", value: funnel.data_stale },
    { label: "AI budget spent", value: funnel.ai_budget_exhausted },
    { label: "Already held", value: funnel.position_open },
    { label: "No slot", value: funnel.capacity_rejected },
  ].filter((r) => r.value > 0);

  return (
    <section className="card scan-funnel">
      <div className="card-head">
        <div>
          <h2>Scan funnel</h2>
          <div className="sub">
            Cycle {scanner?.cycle ?? "—"} · {seconds(scanner?.stage_seconds?.total)} ·
            next {nextScan(scanner?.schedule?.seconds_until_next)}
          </div>
        </div>
      </div>

      <div className="scan-funnel__rows">
        {rows.map((row) => (
          <div className={`scan-funnel__row${row.dim ? " scan-funnel__row--dim" : ""}`} key={row.label}>
            <span className="scan-funnel__label">{row.label}</span>
            {row.note ? <span className="scan-funnel__note">{row.note}</span> : <span />}
            <span className="scan-funnel__value mono">{row.value.toLocaleString()}</span>
            <span className="scan-funnel__bar" aria-hidden>
              <i style={{ width: `${(row.value / funnel.universe_total) * 100}%` }} />
            </span>
          </div>
        ))}
      </div>

      {problems.length ? (
        <div className="scan-funnel__aside">
          {problems.map((row) => (
            <span key={row.label}>
              {row.label} <b className="mono">{row.value}</b>
            </span>
          ))}
        </div>
      ) : null}

      {/* An unbalanced ledger means a name was lost between stages. It is a bug
          in the scanner, not a fact about the market, and the operator should
          not have to read a test run to find out. */}
      {funnel.reconciles === false ? (
        <div className="scan-funnel__warn">
          Funnel does not balance · {funnel.unaccounted} unaccounted
        </div>
      ) : null}

      {funnel.paused_on_full_queue ? (
        <div className="scan-funnel__warn">
          Paused on a full queue · decide or expire a proposal to resume
        </div>
      ) : null}
    </section>
  );
}
