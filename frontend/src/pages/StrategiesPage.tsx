import { useCallback, useEffect, useState } from "react";
import {
  approveStrategy,
  fetchStrategies,
  promoteStrategy,
  recomputeStrategy,
  rejectStrategy,
  type StrategiesPayload,
  type StrategyVersion,
} from "@/lib/api";
import { useDesk } from "@/context/DeskContext";
import { useT } from "@/i18n/I18nProvider";
import type { MessageKey } from "@/i18n";
import { Button } from "@/ui";
import { humanizeError } from "@/lib/messages";

const STAGE_KEYS: Record<string, MessageKey> = {
  proposed: "strategies.stage.proposed",
  backtest_passed: "strategies.stage.backtest",
  oos_passed: "strategies.stage.oos",
  walk_forward_passed: "strategies.stage.wf",
  paper_passed: "strategies.stage.paper",
  human_approved: "strategies.stage.approved",
  production: "strategies.stage.production",
  rejected: "strategies.stage.rejected",
};

function stageLabel(stage: string, t: (k: MessageKey) => string): string {
  const key = STAGE_KEYS[stage];
  return key ? t(key) : stage;
}

function evidenceLine(v: StrategyVersion, t: (k: MessageKey, vars?: Record<string, string | number>) => string): string {
  const ev = v.evidence || {};
  const paper = (ev.paper || {}) as Record<string, unknown>;
  const oos = (ev.out_of_sample || {}) as Record<string, unknown>;
  const bt = (ev.backtest || {}) as Record<string, unknown>;
  if (!ev.recomputed_at) return t("strategies.evidence.none");
  return t("strategies.evidence.summary", {
    bt: Number(bt.trade_count ?? 0),
    oos: Number(oos.oos_trades ?? 0),
    paper: Number(paper.trade_count ?? 0),
    exp:
      paper.expectancy_usd == null || paper.expectancy_usd === undefined
        ? "—"
        : Number(paper.expectancy_usd).toFixed(2),
  });
}

export function StrategiesPage() {
  const t = useT();
  const { showFlash } = useDesk();
  const [payload, setPayload] = useState<StrategiesPayload | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    const next = await fetchStrategies();
    setPayload(next);
  }, []);

  useEffect(() => {
    void load().catch((err) => {
      showFlash(humanizeError(err instanceof Error ? err.message : String(err)));
    });
  }, [load, showFlash]);

  async function run(
    id: string,
    action: "recompute" | "approve" | "promote" | "reject",
  ) {
    setBusy(`${id}:${action}`);
    try {
      if (action === "recompute") await recomputeStrategy(id);
      else if (action === "approve") await approveStrategy(id);
      else if (action === "promote") await promoteStrategy(id);
      else {
        const reason = window.prompt(t("strategies.reject.prompt"));
        if (!reason || reason.trim().length < 3) return;
        await rejectStrategy(id, reason.trim());
      }
      await load();
      showFlash({
        kind: "ok",
        title: t("strategies.flash.ok"),
        detail:
          action === "recompute"
            ? t("strategies.flash.recompute")
            : action === "approve"
              ? t("strategies.flash.approve")
              : action === "promote"
                ? t("strategies.flash.promote")
                : t("strategies.flash.reject"),
      });
    } catch (err) {
      showFlash(humanizeError(err instanceof Error ? err.message : String(err)));
    } finally {
      setBusy(null);
    }
  }

  const versions = payload?.versions ?? [];

  return (
    <div className="strategies-page">
      <section className="card">
        <div className="card-head">
          <div>
            <h2>{t("strategies.title")}</h2>
            <div className="sub">{t("strategies.sub")}</div>
          </div>
        </div>
        <p className="strategies-chain">{t("strategies.chain")}</p>
        {payload?.thresholds ? (
          <p className="strategies-thresholds mono">
            {t("strategies.thresholds", {
              oos: payload.thresholds.min_oos_trades,
              paper: payload.thresholds.min_paper_trades,
              pf: payload.thresholds.min_profit_factor,
              wfe: payload.thresholds.min_walk_forward_efficiency,
            })}
          </p>
        ) : null}
      </section>

      {versions.map((v) => {
        const canApprove = v.stage === "paper_passed";
        const canPromote = v.stage === "human_approved";
        const rejected = v.stage === "rejected";
        const production = v.stage === "production";
        return (
          <section className="card strategies-card" key={v.id}>
            <div className="card-head">
              <div>
                <h2 className="mono">{v.key}</h2>
                <div className="sub">{v.notes || v.name}</div>
              </div>
              <span className={`strategies-stage strategies-stage--${v.stage}`}>
                {stageLabel(v.stage, t)}
              </span>
            </div>
            <p className="strategies-evidence">{evidenceLine(v, t)}</p>
            <p className="strategies-hash mono">
              {t("strategies.hash", { hash: v.parameter_hash.slice(0, 12) })}
            </p>
            {v.approved_by ? (
              <p className="strategies-meta">
                {t("strategies.approvedBy", {
                  who: v.approved_by,
                  when: v.approved_at ?? "—",
                })}
              </p>
            ) : null}
            {v.rejected_reason ? (
              <p className="strategies-meta strategies-meta--bad">{v.rejected_reason}</p>
            ) : null}
            <div className="strategies-actions">
              <Button
                variant="light"
                disabled={!!busy || rejected}
                onClick={() => void run(v.id, "recompute")}
              >
                {t("strategies.action.recompute")}
              </Button>
              <Button
                variant="accent"
                disabled={!!busy || !canApprove}
                onClick={() => void run(v.id, "approve")}
              >
                {t("strategies.action.approve")}
              </Button>
              <Button
                variant="accent"
                disabled={!!busy || !canPromote}
                onClick={() => void run(v.id, "promote")}
              >
                {t("strategies.action.promote")}
              </Button>
              <Button
                variant="ghost"
                disabled={!!busy || rejected || production}
                onClick={() => void run(v.id, "reject")}
              >
                {t("strategies.action.reject")}
              </Button>
            </div>
          </section>
        );
      })}
    </div>
  );
}
