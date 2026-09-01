import { useCallback, useMemo, useState } from "react";

export const DEFAULT_PAGE_SIZE = 10;
export const PAGE_SIZE_OPTIONS = [10, 25, 50, 100] as const;

export type TablePagerState<T> = {
  page: number;
  pageSize: number;
  pageCount: number;
  total: number;
  slice: T[];
  /** Hide controls when the full set fits on one default page. */
  showPager: boolean;
  setPage: (page: number) => void;
  setPageSize: (size: number) => void;
  canPrev: boolean;
  canNext: boolean;
};

export function useTablePager<T>(
  items: readonly T[],
  defaultPageSize: number = DEFAULT_PAGE_SIZE,
): TablePagerState<T> {
  const [pageSize, setPageSizeState] = useState(defaultPageSize);
  const [page, setPageState] = useState(1);
  const total = items.length;
  const pageCount = Math.max(1, Math.ceil(total / pageSize) || 1);
  const pageSafe = Math.min(page, pageCount);

  const slice = useMemo(() => {
    const start = (pageSafe - 1) * pageSize;
    return items.slice(start, start + pageSize) as T[];
  }, [items, pageSafe, pageSize]);

  const setPage = useCallback(
    (next: number) => {
      setPageState(Math.max(1, Math.min(next, pageCount)));
    },
    [pageCount],
  );

  const setPageSize = useCallback((size: number) => {
    setPageSizeState(size);
    setPageState(1);
  }, []);

  return {
    page: pageSafe,
    pageSize,
    pageCount,
    total,
    slice,
    showPager: total >= DEFAULT_PAGE_SIZE,
    setPage,
    setPageSize,
    canPrev: pageSafe > 1,
    canNext: pageSafe < pageCount,
  };
}
