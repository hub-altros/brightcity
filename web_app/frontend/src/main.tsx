import React from "react";
import ReactDOM from "react-dom/client";
// 1. Меняем импорт
import { HashRouter } from "react-router-dom"; 
import { QueryClientProvider } from "@tanstack/react-query";
import { queryClient } from "./lib/queryClient";
import { ThemeProvider } from "./contexts/ThemeContext";
import { ThemedToaster } from "./components/ThemedToaster";
import App from "./App";
import "./index.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ThemeProvider>
      <QueryClientProvider client={queryClient}>
        {/* 2. Меняем компонент и убираем basename */}
        <HashRouter>
          <App />
          <ThemedToaster />
        </HashRouter>
      </QueryClientProvider>
    </ThemeProvider>
  </React.StrictMode>
);