import { StrictMode, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { AuthProvider, useAuth } from "./lib/auth";
import { ssoReady } from "./lib/platformSso";
import type { Role } from "./lib/types";
import DoctorConsole from "./pages/DoctorConsole";
import Home from "./pages/Home";
import Login from "./pages/Login";
import Session from "./pages/Session";
import OtRooms from "./pages/OtRooms";
import { MyPortal, MyVisitSummary } from "./pages/MyPortal";
import { PastSummary, PublicSummary, VisitSummary } from "./pages/Summary";
import "./styles.css";

function Guard({ roles, children }: { roles?: Role[]; children: ReactNode }) {
  const { user, loading } = useAuth();
  if (loading) return <main className="centre-page"><p className="muted">Loading…</p></main>;
  if (!user) return <Navigate to="/login" replace />;
  if (roles && !roles.includes(user.role)) return <Navigate to={user.role === "operator" ? "/" : "/doctor"} replace />;
  return <>{children}</>;
}

function Landing() {
  const { user } = useAuth();
  if (user && user.role !== "operator") return <Navigate to="/doctor" replace />;
  return <Home />;
}

// Awaited, not fire-and-forget: AuthProvider reads the session token once, at mount, so a token set after the first
// render would leave the app looking signed out. Resolves either way; without a platform token nothing changes.
ssoReady.then(() => createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <AuthProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route path="/s/:token" element={<PublicSummary />} />
          <Route path="/my" element={<MyPortal />} />
          <Route path="/my/visits/:visitId" element={<MyVisitSummary />} />
          <Route path="/" element={<Guard><Landing /></Guard>} />
          <Route path="/session/:id" element={<Guard roles={["operator", "admin"]}><Session /></Guard>} />
          <Route path="/session/:id/history/:visitId" element={<Guard roles={["operator", "admin"]}><PastSummary /></Guard>} />
          <Route path="/doctor" element={<Guard roles={["doctor", "admin"]}><DoctorConsole /></Guard>} />
          <Route path="/doctor/:kind/:itemId" element={<Guard roles={["doctor", "admin"]}><DoctorConsole /></Guard>} />
          <Route path="/visits/:id/summary" element={<Guard><VisitSummary /></Guard>} />
          <Route path="/ot" element={<Guard roles={["doctor", "admin"]}><OtRooms /></Guard>} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </BrowserRouter>
    </AuthProvider>
  </StrictMode>,
));
