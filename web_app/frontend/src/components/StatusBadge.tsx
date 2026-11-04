export function StatusBadge({ status, bannerUrl }: { status: string; bannerUrl?: string | null }) {
  let cls = "";
  let label = "";
  if (status === "new") { cls = "status-new"; label = "Новая"; }
  else if (status === "in_progress") { cls = "status-progress"; label = "В работе"; }
  else if (status === "done" && !bannerUrl) { cls = "status-waiting"; label = "Ждёт макет"; }
  else if (status === "done") { cls = "status-done"; label = "Завершена"; }
  else { cls = "status-rejected"; label = "Отклонена"; }
  return (
    <span className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium ${cls}`}>
      <span className="status-dot" />
      {label}
    </span>
  );
}