import { useEffect, useState } from "react";
import { LoadingDots } from "./LoadingDots";
import { SelectField } from "@/ui/SelectField";
import { useT } from "@/i18n/I18nProvider";
import { PAGE_SIZE_OPTIONS, type TablePagerState } from "@/ui/useTablePager";
import styles from "./TablePager.module.css";

type Props = {
  pager: TablePagerState<unknown>;
  loading?: boolean;
};

export function TablePager({ pager, loading = false }: Props) {
  const t = useT();
  const [pending, setPending] = useState<"prev" | "next" | null>(null);
  useEffect(() => { if (!loading) setPending(null); }, [loading]);
  if (!pager.showPager) return null;

  return (
    <div className={styles.Bar} role="navigation" aria-label={t("pager.aria")}>
      <label className={styles.Size}>
        <span className={styles.SizeLabel}>{t("pager.pageSize")}</span>
        <SelectField
          className={styles.SizeSelect}
          ariaLabel={t("pager.pageSize")}
          value={String(pager.pageSize)}
          onChange={(v) => pager.setPageSize(Number(v))}
          options={PAGE_SIZE_OPTIONS.map((n) => ({ value: String(n), label: String(n) }))}
        />
      </label>

      <div className={styles.Nav}>
        <button
          type="button"
          className={styles.NavBtn}
          disabled={loading || !pager.canPrev}
          aria-busy={loading && pending === "prev"}
          onClick={() => { setPending("prev"); pager.setPage(pager.page - 1); }}
        >
          {loading && pending === "prev" ? <LoadingDots ariaLabel={t("common.loading")} /> : null}{t("pager.prev")}
        </button>
        <span className={styles.Page} aria-current="page">
          {pager.page}
        </span>
        <button
          type="button"
          className={styles.NavBtn}
          disabled={loading || !pager.canNext}
          aria-busy={loading && pending === "next"}
          onClick={() => { setPending("next"); pager.setPage(pager.page + 1); }}
        >
          {loading && pending === "next" ? <LoadingDots ariaLabel={t("common.loading")} /> : null}{t("pager.next")}
        </button>
      </div>
    </div>
  );
}
