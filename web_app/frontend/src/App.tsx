import { Navigate, Outlet, Route, Routes } from "react-router-dom";
import { useAdmin } from "./hooks/useAdmin";
import Layout from "./components/Layout";
import Login from "./pages/Login";
import TwoFASetup from "./pages/TwoFASetup";
import TwoFAVerify from "./pages/TwoFAVerify";
import Applications from "./pages/Applications";
import ApplicationDetail from "./pages/ApplicationDetail";
import Gdpr from "./pages/Gdpr";
import Stats from "./pages/Stats";
import { BansPage } from './pages/BansPage';
import { SettingsPage } from './pages/SettingsPage';
import ChatsPage from "./pages/ChatsPage";


function RequireAuth() {
  const { data, isLoading } = useAdmin();
  if (isLoading) {
    return <div className="min-h-screen flex items-center justify-center text-slate-500">Загрузка…</div>;
  }
  if (!data) return <Navigate to="/login" replace />;
  return <Outlet />;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/2fa/setup" element={<TwoFASetup />} />
      <Route path="/2fa/verify" element={<TwoFAVerify />} />

      <Route element={<RequireAuth />}>
        <Route element={<Layout />}>
          <Route index element={<Navigate to="/applications" replace />} />
          <Route path="/applications" element={<Applications />} />
          <Route path="/chats" element={<ChatsPage />} />
          <Route path="/applications/:id" element={<ApplicationDetail />} />
          <Route path="/gdpr" element={<Gdpr />} />
          <Route path="/stats" element={<Stats />} />
          <Route path="/bans" element={<BansPage />} />
          <Route path="/settings" element={<SettingsPage />} />
        </Route>
      </Route>

      <Route path="*" element={<Navigate to="/applications" replace />} />
    </Routes>
  );
}