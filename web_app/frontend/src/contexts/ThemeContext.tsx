import { createContext, useCallback, useContext, useEffect, useState } from 'react';
import type { ReactNode } from 'react';

export type Theme = 'light' | 'dark' | 'system';

interface Ctx {
  theme: Theme;          // текущий выбор пользователя
  resolved: 'light' | 'dark';  // фактически применённая
  setTheme: (t: Theme) => void;
}

const STORAGE_KEY = 'yg_admin_theme_v1';
const C = createContext<Ctx | null>(null);

function _readStored(): Theme {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw === 'light' || raw === 'dark' || raw === 'system') return raw;
  } catch {}
  return 'system';
}

function _systemPrefers(): 'light' | 'dark' {
  if (typeof window === 'undefined') return 'light';
  return window.matchMedia('(prefers-color-scheme: dark)').matches
    ? 'dark'
    : 'light';
}

function _apply(theme: Theme): 'light' | 'dark' {
  const resolved = theme === 'system' ? _systemPrefers() : theme;
  const root = document.documentElement;
  root.classList.toggle('dark', resolved === 'dark');
  // color-scheme управляет нативными скроллбарами, <select>, datepicker и т.п.
  root.style.colorScheme = resolved;
  return resolved;
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<Theme>(_readStored);
  const [resolved, setResolved] = useState<'light' | 'dark'>(
    () => (theme === 'system' ? _systemPrefers() : theme),
  );

  // Применить к <html> при смене темы
  useEffect(() => {
    const r = _apply(theme);
    setResolved(r);
    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch {}
  }, [theme]);

  // Следить за системной темой, если выбран "system"
  useEffect(() => {
    if (theme !== 'system') return;
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    const handler = () => {
      const r = _apply('system');
      setResolved(r);
    };
    mq.addEventListener('change', handler);
    return () => mq.removeEventListener('change', handler);
  }, [theme]);

  const setTheme = useCallback((t: Theme) => setThemeState(t), []);

  return (
    <C.Provider value={{ theme, resolved, setTheme }}>
      {children}
    </C.Provider>
  );
}

export function useTheme() {
  const c = useContext(C);
  if (!c) throw new Error('ThemeProvider missing');
  return c;
}