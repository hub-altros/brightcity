import { useEffect, useState } from "react";
import { Link, useParams, useSearchParams, useNavigate } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { appsApi } from "../api/applications";
import { StatusBadge } from "../components/StatusBadge";
import type { MediaItem } from "../types/api";

// ─── Определение «картинка / PDF / прочее» ────────────────────────────
function isImageUrl(url: string | null | undefined): boolean {
  if (!url) return false;
  const clean = url.split("?")[0].toLowerCase();
  return /\.(jpe?g|png|webp|gif|bmp|heic|avif)$/.test(clean);
}

function isImageMedia(m: MediaItem, forceImage = false): boolean {
  if (forceImage) return true;
  const anyM = m as any;
  if (anyM.kind === "image") return true;
  if (anyM.kind === "doc" || anyM.kind === "pdf" || anyM.kind === "file") return false;
  return isImageUrl(m.url);
}

function isPdfMedia(m: MediaItem, forcePdf = false): boolean {
  if (forcePdf) return true;
  const anyM = m as any;
  if (anyM.kind === "pdf") return true;
  if (anyM.kind === "doc") return false;
  const url = m.open_url || m.url;
  if (!url) return false;
  return url.split("?")[0].toLowerCase().endsWith(".pdf");
}

// ─── Одна миниатюра с подписью ───────────────────────────────────────
function MediaThumb({
  media,
  label,
  onClick,
  forceImage,
  forcePdf,
}: {
  media: MediaItem;
  label: string;
  onClick: () => void;
  forceImage?: boolean;
  forcePdf?: boolean;
}) {
  const src = media.open_url || (media.media_id ? `/media/${media.media_id}` : media.url);
  const isImage = isImageMedia(media, forceImage);
  const isPdf = !isImage && isPdfMedia(media, forcePdf);

  if (!src) {
    return (
      <div className="flex flex-col items-center flex-shrink-0 w-24 md:w-28" title={label}>
        <div className="w-24 h-24 md:w-28 md:h-28 rounded-xl bg-slate-100 dark:bg-slate-800 flex items-center justify-center text-2xl">
          ❌
        </div>
        <span className="mt-1.5 text-[11px] md:text-xs text-slate-400 text-center leading-tight">
          недоступно
        </span>
      </div>
    );
  }

  return (
    <button
      type="button"
      onClick={onClick}
      className="flex flex-col items-center flex-shrink-0 w-24 md:w-28 group text-left"
      title={label}
    >
      <div className="w-24 h-24 md:w-28 md:h-28 rounded-xl overflow-hidden bg-slate-100 dark:bg-slate-800 ring-1 ring-transparent group-hover:ring-2 group-hover:ring-brand-500 transition flex flex-col items-center justify-center">
        {isImage ? (
          <img
            src={src}
            alt={label}
            className="w-full h-full thumb"
            loading="lazy"
            onError={(e) => {
              const img = e.currentTarget;
              img.style.display = "none";
              const parent = img.parentElement;
              if (parent && !parent.querySelector("[data-fallback]")) {
                const span = document.createElement("span");
                span.setAttribute("data-fallback", "1");
                span.style.fontSize = "1.5rem";
                span.textContent = "🖼";
                parent.appendChild(span);
              }
            }}
          />
        ) : (
          <>
            <span className="text-3xl">📄</span>
            <span className="mt-1 text-[10px] text-slate-500 dark:text-slate-400 px-1 text-center truncate w-full">
              {isPdf ? "PDF" : "Файл"}
            </span>
          </>
        )}
      </div>
      <span className="mt-1.5 text-[11px] md:text-xs text-slate-600 dark:text-slate-400 text-center leading-tight">
        {label}
      </span>
    </button>
  );
}

type Group = {
  title: string;
  shortLabel: string;
  items: MediaItem[];
  forceImages?: boolean;
  forcePdf?: boolean;
};

// ─── Все категории в одной компактной сетке ──────────────────────────
function MediaGallery({
  groups,
  onOpen,
}: {
  groups: Group[];
  onOpen: (group: Group, index: number) => void;
}) {
  const nonEmpty = groups.filter((g) => g.items.length > 0);
  if (!nonEmpty.length) return null;

  return (
    <div className="mb-4 md:mb-6 flex flex-wrap gap-x-4 gap-y-3">
      {nonEmpty.map((g) => (
        <div key={g.title} className="flex flex-col min-w-0">
          <div className="text-[11px] uppercase tracking-wider text-slate-500 dark:text-slate-400 mb-1.5 leading-tight">
            {g.title}{" "}
            <span className="text-slate-400 dark:text-slate-500">· {g.items.length}</span>
          </div>
          <div className="flex flex-wrap gap-2 max-w-52 md:max-w-96">
            {g.items.map((m, i) => (
              <MediaThumb
                key={i}
                media={m}
                label={
                  g.items.length > 1
                    ? `${g.shortLabel} ${i + 1}/${g.items.length}`
                    : g.shortLabel
                }
                onClick={() => onOpen(g, i)}
                forceImage={g.forceImages}
                forcePdf={g.forcePdf}
              />
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

// ─── Лайтбокс ────────────────────────────────────────────────────────
function Lightbox({
  group,
  index,
  onClose,
  onPrev,
  onNext,
}: {
  group: Group;
  index: number;
  onClose: () => void;
  onPrev: () => void;
  onNext: () => void;
}) {
  const media = group.items[index];
  const src = media.open_url || (media.media_id ? `/media/${media.media_id}` : media.url);
  const isImage = isImageMedia(media, group.forceImages);
  const isPdf = !isImage && isPdfMedia(media, group.forcePdf);
  const hasPrev = index > 0;
  const hasNext = index < group.items.length - 1;

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      else if (e.key === "ArrowLeft" && hasPrev) onPrev();
      else if (e.key === "ArrowRight" && hasNext) onNext();
    };
    document.addEventListener("keydown", onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [onClose, onPrev, onNext, hasPrev, hasNext]);

  return (
    <div
      className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4 md:p-8"
      onClick={onClose}
    >
      <button
        onClick={(e) => {
          e.stopPropagation();
          onClose();
        }}
        className="absolute top-4 right-4 z-10 w-11 h-11 rounded-full bg-white/10 hover:bg-white/20 text-white text-2xl flex items-center justify-center"
        aria-label="Закрыть"
      >
        ×
      </button>
      <div className="absolute top-4 left-4 z-10 text-white/80 text-sm">
        {group.title} · {index + 1} / {group.items.length}
      </div>
      {hasPrev && (
        <button
          onClick={(e) => {
            e.stopPropagation();
            onPrev();
          }}
          className="absolute left-2 md:left-6 z-10 w-12 h-12 rounded-full bg-white/10 hover:bg-white/20 text-white text-2xl flex items-center justify-center"
          aria-label="Предыдущее"
        >
          ‹
        </button>
      )}
      {hasNext && (
        <button
          onClick={(e) => {
            e.stopPropagation();
            onNext();
          }}
          className="absolute right-2 md:right-6 z-10 w-12 h-12 rounded-full bg-white/10 hover:bg-white/20 text-white text-2xl flex items-center justify-center"
          aria-label="Следующее"
        >
          ›
        </button>
      )}

      {isImage ? (
        <img
          src={src}
          alt={group.title}
          onClick={(e) => e.stopPropagation()}
          className="max-w-full max-h-full object-contain rounded-lg shadow-2xl select-none"
          draggable={false}
        />
      ) : isPdf ? (
        <div
          onClick={(e) => e.stopPropagation()}
          className="w-full h-full max-w-5xl max-h-[90vh] bg-white dark:bg-slate-800 rounded-2xl shadow-2xl flex flex-col overflow-hidden"
        >
          <div className="flex items-center justify-between gap-2 px-3 py-2 border-b border-slate-200 dark:border-slate-700 shrink-0">
            <span className="text-sm font-medium text-slate-900 dark:text-slate-100 truncate">
              {group.title} · {index + 1} / {group.items.length}
            </span>
            <a
              href={src}
              target="_blank"
              rel="noopener noreferrer"
              className="shrink-0 text-xs px-2 py-1 rounded text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-700"
            >
              Открыть в новой вкладке
            </a>
          </div>
          <div className="flex-1 min-h-0 bg-slate-100 dark:bg-slate-900">
            <iframe src={src} title="PDF" className="w-full h-full border-0" />
          </div>
        </div>
      ) : (
        <div
          onClick={(e) => e.stopPropagation()}
          className="bg-white dark:bg-slate-800 rounded-2xl p-6 md:p-8 max-w-md w-full shadow-2xl text-center"
        >
          <div className="text-6xl mb-4">📄</div>
          <div className="font-semibold mb-2 text-slate-900 dark:text-slate-100">
            {group.title} · {index + 1} / {group.items.length}
          </div>
          <p className="text-sm text-slate-500 dark:text-slate-400 mb-4">
            Этот файл нельзя показать здесь. Откройте его в новой вкладке.
          </p>
          <a
            href={src}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-block px-4 py-2 rounded-lg bg-brand-500 hover:bg-brand-600 text-white text-sm font-semibold"
          >
            Открыть файл
          </a>
        </div>
      )}
    </div>
  );
}

export default function ApplicationDetail() {
  const { id } = useParams<{ id: string }>();
  const appId = Number(id);
  const qc = useQueryClient();
  const [replyText, setReplyText] = useState("");
  const [rejectReason, setRejectReason] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [lightbox, setLightbox] = useState<{
    group: Group;
    index: number;
  } | null>(null);

  const [sp] = useSearchParams();
  const navigate = useNavigate();
  const listParams = Object.fromEntries(sp);

  const goto = (newId: number | null | undefined) => {
    if (!newId) return;
    const qs = sp.toString();
    navigate(`/applications/${newId}${qs ? "?" + qs : ""}`);
  };

  const { data: item, isLoading } = useQuery({
    queryKey: ["application", appId, listParams],
    queryFn: () => appsApi.get(appId, listParams),
    enabled: Number.isFinite(appId),
  });

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement) return;
      if (e.target instanceof HTMLTextAreaElement) return;
      if (!item) return;
      
      if (e.key === "ArrowLeft" && item.prev_id) goto(item.prev_id);
      if (e.key === "ArrowRight" && item.next_id) goto(item.next_id);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [item?.prev_id, item?.next_id, sp]);

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["application", appId] });
    qc.invalidateQueries({ queryKey: ["applications"] });
  };

  const setInProgress = useMutation({
    mutationFn: () => appsApi.setInProgress(appId),
    onSuccess: () => {
      toast.success("Взято в работу");
      invalidate();
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const reject = useMutation({
    mutationFn: () => appsApi.reject(appId, rejectReason),
    onSuccess: () => {
      toast.success("Отклонено");
      setRejectReason("");
      invalidate();
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const reply = useMutation({
    mutationFn: () => appsApi.reply(appId, replyText),
    onSuccess: () => {
      toast.success("Отправлено");
      setReplyText("");
      invalidate();
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const uploadBanner = useMutation({
    mutationFn: (f: File) => appsApi.uploadBanner(appId, f),
    onSuccess: () => {
      toast.success("Макет отправлен");
      setFile(null);
      invalidate();
    },
    onError: (e: Error) => toast.error(e.message),
  });

  if (isLoading || !item) {
    return (
      <div className="max-w-7xl mx-auto p-4 text-slate-500 dark:text-slate-400">
        Загрузка…
      </div>
    );
  }

  const bannerItems: MediaItem[] = [];
  if (item.banner_media_id) {
    bannerItems.push({
      url: item.banner_url || "",
      media_id: item.banner_media_id,
      open_url: (item as any).banner_open_url || item.banner_url,
    });
  } else if (
    item.banner_url &&
    (item.banner_url.startsWith("http") ||
      item.banner_url.startsWith("/media/") ||
      item.banner_url.startsWith("photo-"))
  ) {
    bannerItems.push({ url: item.banner_url, media_id: null, open_url: item.banner_url });
  }

  const groups: Group[] = [
    { title: "📸 Фото ребёнка", shortLabel: "Фото", items: item.child_photos, forceImages: true },
    { title: "🖼 Рисунки", shortLabel: "Рисунок", items: item.drawings, forceImages: true },
    { title: "💳 Квитанции", shortLabel: "Квитанция", items: item.payment_docs, forcePdf: true },
    { title: "📎 О себе", shortLabel: "Вложение", items: item.org_attachments },
    { title: "🎨 Макет", shortLabel: "Макет", items: bannerItems, forceImages: true },
  ];

  const showSendBanner = item.status === "in_progress" && !item.banner_url;
  const showReject = item.status !== "rejected" && item.status !== "done";

  return (
    <>
      <header className="md:hidden sticky top-0 z-20 bg-white/95 dark:bg-slate-900/95 backdrop-blur border-b border-slate-200 dark:border-slate-800 safe-top">
        <div className="flex items-center gap-1 px-2 py-2">
          <Link
            to={`/applications${sp.toString() ? "?" + sp.toString() : ""}`}
            className="w-11 h-11 rounded-full hover:bg-slate-100 dark:hover:bg-slate-800 flex items-center justify-center text-xl text-slate-900 dark:text-slate-100"
          >
            ←
          </Link>
          <div className="font-bold text-slate-900 dark:text-slate-100">Заявка #{item.id}</div>
          
          <div className="flex items-center gap-1 ml-2">
            <button type="button" onClick={() => goto(item.prev_id)} disabled={!item.prev_id}
              className="w-8 h-8 flex items-center justify-center rounded-lg text-slate-700 dark:text-slate-200 hover:bg-slate-100 dark:hover:bg-slate-800 disabled:opacity-30 disabled:cursor-not-allowed">←</button>
            <button type="button" onClick={() => goto(item.next_id)} disabled={!item.next_id}
              className="w-8 h-8 flex items-center justify-center rounded-lg text-slate-700 dark:text-slate-200 hover:bg-slate-100 dark:hover:bg-slate-800 disabled:opacity-30 disabled:cursor-not-allowed">→</button>
          </div>

          <span className="ml-auto mr-2">
            <StatusBadge status={item.status} bannerUrl={item.banner_url} />
          </span>
        </div>
        {(["new", "rejected"] as string[]).includes(item.status) && (
          <div className="px-3 pb-2">
            <button
              onClick={() => setInProgress.mutate()}
              disabled={setInProgress.isPending}
              className="w-full px-4 py-2.5 rounded-lg bg-slate-900 dark:bg-slate-100 text-white dark:text-slate-900 text-sm font-semibold disabled:opacity-50"
            >
              {item.status === "rejected" ? "↩ Вернуть в работу" : "В работу"}
            </button>
          </div>
        )}
      </header>

      <div className="max-w-7xl mx-auto p-4 md:p-6 pb-8">
        <div className="hidden md:flex items-start justify-between gap-4 mb-6">
          <div>
            <div className="flex items-center gap-3 mb-1">
              <Link
                to={`/applications${sp.toString() ? "?" + sp.toString() : ""}`}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium text-slate-600 bg-slate-100 hover:bg-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:hover:bg-slate-700 transition-colors"
                title="Вернуться к списку"
              >
                <span>←</span> Назад
              </Link>
              <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100">Заявка #{item.id}</h1>
              
              <div className="flex items-center gap-1 ml-1">
                <button
                  type="button"
                  onClick={() => goto(item.prev_id)}
                  disabled={!item.prev_id}
                  title="Предыдущая заявка (←)"
                  className="w-8 h-8 flex items-center justify-center rounded-lg border border-slate-300 dark:border-slate-600 text-slate-700 dark:text-slate-200 hover:bg-slate-100 dark:hover:bg-slate-700 disabled:opacity-40 disabled:cursor-not-allowed transition"
                >
                  ←
                </button>
                <button
                  type="button"
                  onClick={() => goto(item.next_id)}
                  disabled={!item.next_id}
                  title="Следующая заявка (→)"
                  className="w-8 h-8 flex items-center justify-center rounded-lg border border-slate-300 dark:border-slate-600 text-slate-700 dark:text-slate-200 hover:bg-slate-100 dark:hover:bg-slate-700 disabled:opacity-40 disabled:cursor-not-allowed transition"
                >
                  →
                </button>
              </div>
            </div>
            <p className="text-sm text-slate-500 dark:text-slate-400 mt-1 flex items-center gap-2 flex-wrap pl-[4.5rem]">
              Создана {new Date(item.created_at).toLocaleString("ru-RU")} ·{" "}
              <StatusBadge status={item.status} bannerUrl={item.banner_url} />
            </p>
          </div>
          {(["new", "rejected"] as string[]).includes(item.status) && (
            <button
              onClick={() => setInProgress.mutate()}
              disabled={setInProgress.isPending}
              className="shrink-0 px-4 py-2 rounded-lg bg-slate-900 dark:bg-slate-100 text-white dark:text-slate-900 text-sm font-medium disabled:opacity-50"
            >
              {item.status === "rejected" ? "↩ В работу" : "В работу"}
            </button>
          )}
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 md:gap-6 mb-4 md:mb-6">
          <div className="lg:col-span-2">
            <div className="bg-white dark:bg-slate-800 rounded-2xl border border-slate-200 dark:border-slate-700 p-4 md:p-6 h-full">
              <h2 className="font-bold mb-4 text-slate-900 dark:text-slate-100">Данные заявки</h2>
              <dl className="grid grid-cols-2 gap-x-4 gap-y-4 text-sm">
                <div>
                  <dt className="text-xs text-slate-500 dark:text-slate-400 mb-1">Город</dt>
                  <dd className="font-medium text-slate-900 dark:text-slate-100">{item.location || "—"}</dd>
                </div>
                <div>
                  <dt className="text-xs text-slate-500 dark:text-slate-400 mb-1">Локация</dt>
                  <dd className="font-medium text-slate-900 dark:text-slate-100">
                    {item.placement_location || "—"}
                  </dd>
                </div>
                <div>
                  <dt className="text-xs text-slate-500 dark:text-slate-400 mb-1">Тип</dt>
                  <dd className="font-medium text-slate-900 dark:text-slate-100">{item.user_type || "—"}</dd>
                </div>
                <div>
                  <dt className="text-xs text-slate-500 dark:text-slate-400 mb-1">Телефон</dt>
                  <dd className="font-medium text-slate-900 dark:text-slate-100">{item.phone || "—"}</dd>
                </div>
                <div>
                  <dt className="text-xs text-slate-500 dark:text-slate-400 mb-1">Ребёнок</dt>
                  <dd className="font-medium text-slate-900 dark:text-slate-100">{item.name_age || "—"}</dd>
                </div>
                <div>
                  <dt className="text-xs text-slate-500 dark:text-slate-400 mb-1">Формат</dt>
                  <dd className="font-medium text-slate-900 dark:text-slate-100">{item.format_choice || "—"}</dd>
                </div>
                <div className="col-span-2">
                  <dt className="text-xs text-slate-500 dark:text-slate-400 mb-1">Обучение</dt>
                  <dd className="font-medium text-slate-900 dark:text-slate-100">{item.training_info || "—"}</dd>
                </div>
              </dl>
            </div>
          </div>

          <div className="space-y-4 md:space-y-6">
            <div className="bg-white dark:bg-slate-800 rounded-2xl border border-slate-200 dark:border-slate-700 p-4 md:p-6">
              <h2 className="font-bold mb-3 text-slate-900 dark:text-slate-100">Служебное</h2>
              <dl className="text-sm space-y-2">
                <div className="flex justify-between">
                  <dt className="text-slate-500 dark:text-slate-400">User ID</dt>
                  <dd className="font-mono text-xs text-slate-900 dark:text-slate-100">{item.user_id ?? "—"}</dd>
                </div>
                <div className="flex justify-between">
                  <dt className="text-slate-500 dark:text-slate-400">Платформа</dt>
                  <dd className="text-slate-900 dark:text-slate-100 uppercase">{item.platform || "—"}</dd>
                </div>
                <div className="flex justify-between">
                  <dt className="text-slate-500 dark:text-slate-400">Согласие</dt>
                  <dd className="text-slate-900 dark:text-slate-100">
                    {item.consent_version ? `✅ ${item.consent_version}` : "—"}
                  </dd>
                </div>
              </dl>
            </div>

            <div className="bg-white dark:bg-slate-800 rounded-2xl border border-slate-200 dark:border-slate-700 p-4 md:p-6">
              <h2 className="font-bold mb-3 text-slate-900 dark:text-slate-100">История</h2>
              <ol className="space-y-2 text-sm">
                {item.history.map((h, i) => (
                  <li key={i} className="flex gap-2">
                    <span className="w-2 h-2 rounded-full bg-brand-500 mt-1.5 flex-shrink-0" />
                    <div>
                      <div className="text-slate-900 dark:text-slate-100">
                        {h.old_status || "—"} → {h.new_status}
                      </div>
                      <div className="text-xs text-slate-500 dark:text-slate-400">
                        {h.admin_name || "—"} ·{" "}
                        {new Date(h.changed_at).toLocaleString("ru-RU", {
                          day: "2-digit",
                          month: "2-digit",
                          hour: "2-digit",
                          minute: "2-digit",
                        })}
                      </div>
                    </div>
                  </li>
                ))}
              </ol>
            </div>
          </div>
        </div>

        <MediaGallery
          groups={groups}
          onOpen={(group, index) => setLightbox({ group, index })}
        />

        {(showSendBanner || showReject) && (
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 md:gap-6 mb-4 md:mb-6">
            {showSendBanner && (
              <div className="bg-white dark:bg-slate-800 rounded-2xl border border-brand-200 dark:border-brand-900/50 p-4 md:p-6">
                <h2 className="font-bold mb-3 text-slate-900 dark:text-slate-100">📤 Отправить макет</h2>
                <p className="text-sm text-slate-500 dark:text-slate-400 mb-4">
                  JPEG, PNG или WebP до 15 МБ. Пересохранится в JPEG без EXIF.
                </p>
                <form
                  onSubmit={(e) => {
                    e.preventDefault();
                    if (file) uploadBanner.mutate(file);
                  }}
                  className="flex flex-col sm:flex-row items-stretch sm:items-center gap-2"
                >
                  <input
                    type="file"
                    accept="image/jpeg,image/png,image/webp"
                    onChange={(e) => setFile(e.target.files?.[0] || null)}
                    className="w-full min-w-0 flex-1 border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm bg-white dark:bg-slate-900 text-slate-900 dark:text-slate-100 file:mr-3 file:py-1.5 file:px-3 file:rounded-md file:border-0 file:bg-slate-100 dark:file:bg-slate-700 file:text-slate-700 dark:file:text-slate-200 file:text-sm hover:file:bg-slate-200 dark:hover:file:bg-slate-600 transition-colors cursor-pointer"
                  />
                  <button
                    type="submit"
                    disabled={!file || uploadBanner.isPending}
                    className="shrink-0 px-4 py-2 rounded-lg bg-brand-500 hover:bg-brand-600 text-white text-sm font-semibold disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                  >
                    {uploadBanner.isPending ? "Отправка…" : "Отправить"}
                  </button>
                </form>
              </div>
            )}

            {showReject && (
              <div className="bg-white dark:bg-slate-800 rounded-2xl border border-red-200 dark:border-red-900/50 p-4 md:p-6">
                <h2 className="font-bold mb-3 text-red-700 dark:text-red-400">Отклонить</h2>
                <p className="text-sm text-slate-500 dark:text-slate-400 mb-4">
                  Причина будет отправлена пользователю.
                </p>
                <form
                  onSubmit={(e) => {
                    e.preventDefault();
                    if (rejectReason.trim()) reject.mutate();
                  }}
                  className="flex flex-col sm:flex-row items-stretch sm:items-center gap-2"
                >
                  <input
                    value={rejectReason}
                    onChange={(e) => setRejectReason(e.target.value)}
                    placeholder="Причина отклонения"
                    className="w-full min-w-0 flex-1 border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm bg-white dark:bg-slate-900 text-slate-900 dark:text-slate-100 placeholder:text-slate-400 dark:placeholder:text-slate-500"
                  />
                  <button
                    type="submit"
                    disabled={reject.isPending}
                    className="shrink-0 px-4 py-2 rounded-lg bg-red-600 hover:bg-red-700 text-white text-sm font-medium disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                  >
                    Отклонить
                  </button>
                </form>
              </div>
            )}
          </div>
        )}

        <div className="bg-white dark:bg-slate-800 rounded-2xl border border-slate-200 dark:border-slate-700 p-4 md:p-6">
          <div className="flex items-center justify-between mb-4">
            <h2 className="font-bold text-slate-900 dark:text-slate-100">Переписка</h2>
            {item.messages.length > 0 && (
              <span className="text-xs text-slate-500 dark:text-slate-400">
                {item.messages.length === 50
                  ? "последние 50"
                  : `${item.messages.length} сообщ.`}
              </span>
            )}
          </div>

          <form
            onSubmit={(e) => {
              e.preventDefault();
              if (replyText.trim()) reply.mutate();
            }}
            className="flex gap-2 mb-4 pb-4 border-b border-slate-100 dark:border-slate-700"
          >
            <input
              value={replyText}
              onChange={(e) => setReplyText(e.target.value)}
              placeholder="Написать сообщение…"
              className="w-full min-w-0 flex-1 border border-slate-300 dark:border-slate-600 rounded-full px-4 py-2.5 text-sm bg-white dark:bg-slate-900 text-slate-900 dark:text-slate-100 placeholder:text-slate-400 dark:placeholder:text-slate-500"
            />
            <button
              type="submit"
              disabled={reply.isPending}
              className="shrink-0 w-11 h-11 rounded-full bg-brand-500 hover:bg-brand-600 text-white disabled:opacity-50 flex items-center justify-center transition-colors"
              aria-label="Отправить"
            >
              →
            </button>
          </form>

          <div className="space-y-3 text-sm max-h-96 overflow-y-auto pr-1">
            {item.messages.length === 0 && (
              <div className="text-slate-400 dark:text-slate-500 text-sm">Сообщений пока нет.</div>
            )}
            {item.messages.map((m, i) => (
              <div
                key={i}
                className={`flex gap-2 ${m.direction === "out" ? "justify-end" : ""}`}
              >
                <div
                  className={`rounded-2xl px-3 py-2 max-w-[78%] ${
                    m.direction === "out"
                      ? "bg-brand-500 text-white"
                      : "bg-slate-100 dark:bg-slate-700 text-slate-900 dark:text-slate-100"
                  }`}
                >
                  <div
                    className={`text-[11px] mb-0.5 ${
                      m.direction === "out" ? "text-brand-100" : "text-slate-500 dark:text-slate-400"
                    }`}
                  >
                    {m.direction === "out" ? "Вы" : "Пользователь"} ·{" "}
                    {new Date(m.created_at).toLocaleString("ru-RU", {
                      day: "2-digit",
                      month: "2-digit",
                      hour: "2-digit",
                      minute: "2-digit",
                    })}
                  </div>
                  <div className="whitespace-pre-wrap break-words">{m.text}</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>

      {lightbox && (
        <Lightbox
          group={lightbox.group}
          index={lightbox.index}
          onClose={() => setLightbox(null)}
          onPrev={() =>
            setLightbox((lb) =>
              lb && lb.index > 0 ? { ...lb, index: lb.index - 1 } : lb
            )
          }
          onNext={() =>
            setLightbox((lb) =>
              lb && lb.index < lb.group.items.length - 1
                ? { ...lb, index: lb.index + 1 }
                : lb
            )
          }
        />
      )}
    </>
  );
}