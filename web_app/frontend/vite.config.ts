import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";

// Для GitHub Pages: hub-altros.github.io/brightcity/
// Для локальной разработки (dev): /  — Vite сам отдаёт с корня
const IS_PROD = process.env.NODE_ENV === "production";
const BASE = "/brightcity/";

export default defineConfig({
  base: IS_PROD ? BASE : "/",

  plugins: [
    react(),
    VitePWA({
      // Временно отключено для GitHub Pages.
      // После запуска Pages включим и настроим start_url/scope/icons
      // относительно BASE.
      disable: true,
      registerType: "autoUpdate",
      includeAssets: [
        "favicon.ico",
        "icons/icon-192.png",
        "icons/icon-512.png",
      ],
      manifest: {
        name: "Яркий!Город — Админка",
        short_name: "Яркий!Админ",
        description: "Управление заявками проекта «Яркий!Город»",
        start_url: `${BASE}applications`,
        scope: BASE,
        display: "standalone",
        orientation: "portrait-primary",
        background_color: "#f8fafc",
        theme_color: "#ea580c",
        icons: [
          { src: `${BASE}icons/icon-192.png`, sizes: "192x192", type: "image/png" },
          { src: `${BASE}icons/icon-512.png`, sizes: "512x512", type: "image/png" },
          {
            src: `${BASE}icons/icon-512.png`,
            sizes: "512x512",
            type: "image/png",
            purpose: "any maskable",
          },
        ],
      },
      workbox: {
        navigateFallbackDenylist: [/^\/api/, /^\/media/],
        runtimeCaching: [
          {
            urlPattern: /\/media\//,
            handler: "CacheFirst",
            options: {
              cacheName: "media-cache",
              expiration: { maxEntries: 200, maxAgeSeconds: 86400 },
            },
          },
        ],
      },
    }),
  ],

  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/media": "http://127.0.0.1:8000",
    },
  },

  build: {
    // Для GitHub Pages: dist — рядом с package.json.
    // Старый путь "../static/dist" — под FastAPI-раздачу, больше не нужен.
    outDir: "dist",
    emptyOutDir: true,
  },
});