import { useDesk } from "@/context/DeskContext";
import { useT } from "@/i18n/I18nProvider";
import type { MessageKey } from "@/i18n";

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

type Row = { labelKey: MessageKey; value: number; noteKey?: MessageKey; dim?: boolean };

function seconds(value: number | undefined): string {
  if (value == null) return "—";
  if (value < 1) return `${Math.round(value * 1000)}ms`;
  return `${value.toFixed(value < 10 ? 1 : 0)}s`;
}

function nextScan(secondsUntil: number | undefined, t: ReturnType<typeof useT>): string {
  if (secondsUntil == null) return "—";
  if (secondsUntil <= 0) return t("funnel.nextDue");
  const at = new Date(Date.now() + secondsUntil * 1000);
  return at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function ScanFunnelCard() {
  const t = useT();
  const { desk } = useDesk();
  const scanner = desk?.scanner;
  const funnel = scanner?.funnel;

  if (!funnel || !funnel.universe_total) {
    return (
      <section className="card scan-funnel">
        <div className="card-head">
          <div>
            <h2>{t("funnel.title")}</h2>
            <div className="sub">{t("funnel.empty")}</div>
          </div>
        </div>
      </section>
    );
  }

  const rows: Row[] = [
    { labelKey: "funnel.row.universe", value: funnel.universe_total },
    { labelKey: "funnel.row.eligible", value: funnel.structurally_eligible, noteKey: "funnel.note.structural" },
    { labelKey: "funnel.row.market", value: funnel.market_filter_passed, noteKey: "funnel.note.liquidity" },
    { labelKey: "funnel.row.quant", value: funnel.quant_shortlisted, noteKey: "funnel.note.topk" },
    { labelKey: "funnel.row.deep", value: funnel.deep_analysis_started, noteKey: "funnel.note.expensive" },
    { labelKey: "funnel.row.risk", value: funnel.risk_passed },
    { labelKey: "funnel.row.published", value: funnel.published },
    { labelKey: "funnel.row.outranked", value: funnel.final_outranked, dim: true },
  ];

  const problems: Row[] = (
    [
      { labelKey: "funnel.problem.provider" as const, value: funnel.provider_failed },
      { labelKey: "funnel.problem.stale" as const, value: funnel.data_stale },
      { labelKey: "funnel.problem.aiBudget" as const, value: funnel.ai_budget_exhausted },
      { labelKey: "funnel.problem.held" as const, value: funnel.position_open },
      { labelKey: "funnel.problem.noSlot" as const, value: funnel.capacity_rejected },
    ] satisfies Row[]
  ).filter((r) => r.value > 0);

  return (
    <section className="card scan-funnel">
      <div className="card-head">
        <div>
          <h2>{t("funnel.title")}</h2>
          <div className="sub">
            {t("funnel.sub", {
              n: scanner?.cycle ?? "—",
              dur: seconds(scanner?.stage_seconds?.total),
              t: nextScan(scanner?.schedule?.seconds_until_next, t),
            })}
          </div>
        </div>
      </div>

      <div className="scan-funnel__rows">
        {rows.map((row) => (
          <div
            className={`scan-funnel__row${row.dim ? " scan-funnel__row--dim" : ""}`}
            key={row.labelKey}
          >
            <span className="scan-funnel__label">{t(row.labelKey)}</span>
            {row.noteKey ? (
              <span className="scan-funnel__note">{t(row.noteKey)}</span>
            ) : (
              <span />
            )}
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
            <span key={row.labelKey}>
              {t(row.labelKey)} <b className="mono">{row.value}</b>
            </span>
          ))}
        </div>
      ) : null}

      {/* An unbalanced ledger means a name was lost between stages. It is a bug
          in the scanner, not a fact about the market, and the operator should
          not have to read a test run to find out. */}
      {funnel.reconciles === false ? (
        <div className="scan-funnel__warn">
          {t("funnel.warn.unbalanced", { n: funnel.unaccounted })}
        </div>
      ) : null}

      {funnel.paused_on_full_queue ? (
        <div className="scan-funnel__warn">{t("funnel.warn.fullQueue")}</div>
      ) : null}
    </section>
  );
}
