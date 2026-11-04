const API_URL = import.meta.env.VITE_API_URL || "";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const isJson = !init?.body || typeof init.body === "string";
  const url = `${API_URL}${path}`;
  const res = await fetch(url, {
    ...init,
    credentials: "include",
    headers: {
      ...(isJson ? { "Content-Type": "application/json" } : {}),
      ...(init?.headers || {}),
    },
  });
  if (res.status === 401) {
    if (!path.startsWith("/api/auth/")) {
      // На GitHub Pages login живёт по /<repo>/login, а не /login.
      // import.meta.env.BASE_URL = '/brightcity/' (из vite.config.ts base)
      window.location.href = `${import.meta.env.BASE_URL}login`;
    }
    throw new ApiError(401, "unauthorized");
  }
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      if (body?.detail) msg = body.detail;
    } catch {}
    throw new ApiError(res.status, msg);
  }
  if (res.status === 204) return undefined as unknown as T;
  return (await res.json()) as T;
}