export type Role = "admin" | "doctor" | "operator";

export interface User {
  id: string;
  email: string;
  full_name: string;
  role: Role;
  registration_no?: string | null;
}

export interface Message {
  id: string;
  seq: number;
  role: "user" | "assistant" | "doctor" | "system";
  content: string;
  meta: Record<string, any>;
  created_at: string;
}

export type Stage =
  | "identify" | "concern" | "assess" | "capture" | "analyse" | "recommend" | "approval" | "booking" | "wrap_up" | "done"
  | "handed_over";

export interface Slot {
  room_id: string;
  room_code: string;
  room_name: string;
  starts_at: string;
  ends_at: string;
  label: string;
}

export interface Appointment {
  id?: string;
  procedure: string;
  status: "tentative" | "confirmed" | "deferred" | "cancelled";
  label: string | null;
  starts_at: string | null;
  ends_at: string | null;
  room_name: string | null;
  session_index: number;
  sessions_total: number;
}

export interface UiHints {
  stage: Stage;
  mode: "ai" | "handed_over" | "closed";
  step: number | null;
  steps: number;
  input: "text" | "tel" | "otp" | "date";
  quick_replies: string[];
  question: { id: string; text: string; index: number; total: number; type: string } | null;
  camera: { pose: "front" | "left" | "right"; prompt: string; missing: string[]; done: string[] } | null;
  client_first_name: string | null;
  visit_id: string | null;
  programme: string | null;
  session_number: number | null;
  results_available: boolean;
  approval_status: string | null;
  summary_available: boolean;
  history_count?: number;
  unread_messages?: number;
  booking?: { procedure: string; duration_min: number; sessions: number; remaining: number; slots: Slot[] };
}

export interface TurnPayload {
  conversation_id: string;
  messages: Message[];
  ui: UiHints;
  duplicate?: boolean;
  quality?: { accepted: boolean; guidance: string | null; issues: string[]; missing_poses: string[] } | null;
  photo_ok?: boolean;
}

export interface PlanItem {
  catalogue_id: string;
  kind: "service" | "product" | "doctor_service";
  name: string;
  sessions: number | null;
  instructions: string | null;
  reason: string | null;
  price_inr?: number;
}

export interface Finding {
  concern: string;
  label: string;
  score: number;
  source?: "model" | "estimate";  // scored from the skin model's findings, or an estimate (no model for it yet)
  count?: number;
}

// A picture from the clinic's skin models, as doctors and admins see it.
export interface ModelImageStaff {
  id: string;
  kind: "diagnosis_overlay" | "post_treatment_preview";
  pose: string;
  status: "pending" | "ready" | "failed";
  error: string | null;
  model: string;
  has_before: boolean;
  found: Record<string, number>;
  face_detected: boolean | null;
  findings_sent: boolean;
  warning: string | null;  // e.g. the service didn't report findings, or sent a picture of the wrong size
  changed: boolean | null;
  seconds: number | null;
  caption: string;
  created_at: string;
}

// As the client sees it (only after approval).
export interface ModelImageClient {
  id: string;
  kind: "diagnosis_overlay" | "post_treatment_preview";
  pose: string;
  has_before: boolean;
  title: string;
  caption: string;
}

// Where a picture's file is fetched from, for the screen showing it (staff, the client's phone, a shared link).
export type ImageSource = (id: string, variant?: "image" | "before") => { path: string; token?: string | null; auth?: boolean };

export interface CatalogueItem {
  id: string;
  kind: string;
  name: string;
  default_sessions: number;
  concerns: string[];
  contraindications: string[];
  price_inr: number;
  usage: string;
}

// ── Follow-up chats ──────────────────────────────────────────────────────────
export interface ThreadMessage {
  id: string;
  seq: number;
  role: "client" | "assistant" | "staff" | "system";
  content: string;
  author: string | null;
  author_role: "doctor" | "clinic team" | null;
  channel: string | null;
  created_at: string;
}

export interface ThreadSummary {
  id: string;
  visit_id: string | null;
  title: string;
  date: string | null;
  mode: "ai" | "staff";
  waiting: boolean;
  unread: number;
  last_message: { role: string; author: string | null; content: string; created_at: string } | null;
  last_message_at: string | null;
}

export interface StaffThread extends ThreadSummary {
  client: { id: string; full_name: string; age: number | null; gender: string | null; mobile: string };
  waiting_since: string | null;
  handover_reason: string | null;
  handover_text: string | null;
  handover_details: string | null;
  assigned_to: string | null;
  visit: { id: string; status: string; programme: string; session_number: number; date: string } | null;
}

export interface ThreadView<T = ThreadSummary> {
  thread: T;
  messages: ThreadMessage[];
}
