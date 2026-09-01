/** 1px green marching-ants outline that follows rounded corners. */
export function WorkingAntsBorder({ radius = 12 }: { radius?: number }) {
  return (
    <svg className="ag-ants" aria-hidden>
      <rect rx={radius} ry={radius} pathLength={100} />
    </svg>
  );
}
