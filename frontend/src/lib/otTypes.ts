export interface Readiness { complete: boolean; open_mandatory: number; blocked: number; total: number; done: number }

export interface ChecklistItem {
  key: string;
  label: string;
  mandatory: boolean;
  status: "pending" | "done" | "not_applicable" | "blocked";
  note: string | null;
  updated_at: string | null;
  updated_by: string | null;
}

export type Stage = "scheduled" | "checked_in" | "in_progress" | "completed" | "no_show";

export interface OtBooking {
  id: string;
  visit_id: string;
  item_id: string;
  procedure: string;
  status: "tentative" | "confirmed" | "deferred" | "cancelled";
  tentative: boolean;
  session_index: number;
  sessions_total: number;
  source: string;
  room_id: string | null;
  room_name: string | null;
  room_code: string | null;
  starts_at: string;
  ends_at: string;
  clear_until: string;
  label: string;
  client_name?: string;
  doctor_id: string | null;
  doctor_name: string | null;
  equipment_id: string | null;
  equipment_name: string | null;
  stage: Stage;
  version: number;
  confirmed_at: string | null;
  confirmed_by: string | null;
  checked_in_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  override_reason: string | null;
  overridden_rules: string[];
  cancel_reason: string | null;
  readiness?: Readiness;
}

export interface BookingDetail extends OtBooking {
  checklist: ChecklistItem[];
  needs_doctor: boolean;
  equipment_type: string | null;
  duration_min: number | null;
}

export interface OtBlock {
  id: string;
  target: "room" | "doctor" | "equipment";
  target_id: string;
  target_name: string | null;
  room_id: string | null;
  starts_at: string;
  ends_at: string;
  reason: string;
}

export interface OtRoom {
  id: string;
  code: string;
  name: string;
  capabilities: string[];
  open_time: string;
  close_time: string;
  open_days: number[];
  buffer_minutes: number;
  active: boolean;
  open_today?: boolean;
  bookings?: OtBooking[];
  blocks?: OtBlock[];
}

export interface ToSchedule {
  visit_id: string;
  client_name: string;
  item_id: string;
  procedure: string;
  duration_min: number;
  sessions: number;
  deferred: boolean;
  visit_date: string;
}

export interface Schedule {
  day: string;
  timezone: string;
  rooms: (OtRoom & { open_today: boolean; bookings: OtBooking[]; blocks: OtBlock[] })[];
  to_schedule: ToSchedule[];
  to_confirm: OtBooking[];
  holiday: string | null;
  other_blocks: OtBlock[];
  tentative: number;
}

export interface Equipment { id: string; code: string; name: string; equipment_type: string; buffer_minutes: number; active: boolean }
export interface DoctorRow { id: string; full_name: string; active: boolean; hours: { weekday: number; start_time: string; end_time: string }[] }
export interface Holiday { day: string; name: string }

export interface Conflict { code: string; message: string; overridable: boolean }

export interface Rate { value: number | null; num: number; den: number }
export interface RoomStats {
  available_min: number; booked_min: number; actual_min: number;
  booked_utilisation: Rate; actual_utilisation: Rate;
  bookings: number; tentative: number; confirmed: number; completed: number; no_show: number; cancelled: number;
  cancel_reasons: [string, number][];
  no_show_rate: Rate; on_time_start: Rate; overruns: number; avg_overrun_min: number | null;
  turnover: { count: number; avg_min: number | null; median_min: number | null };
  overrides: number;
}
export interface Report {
  from: string; to: string; timezone: string;
  total: RoomStats;
  rooms: (RoomStats & { room_id: string; code: string; name: string; buffer_minutes: number; active: boolean })[];
  by_procedure: { procedure: string; bookings: number; minutes: number }[];
  by_equipment: { equipment: string; bookings: number; minutes: number }[];
  by_doctor: { doctor: string; bookings: number; minutes: number }[];
  equipment_downtime_min: number;
  holidays: string[];
}

export const CAPABILITY: Record<string, string> = {
  laser: "Laser", led: "LED", peel: "Peels", facial: "Facials", minor_procedure: "Minor procedures",
};

export const STAGE_LABEL: Record<Stage, string> = {
  scheduled: "Scheduled", checked_in: "Checked in", in_progress: "In treatment", completed: "Done", no_show: "No-show",
};

export const RULE_LABEL: Record<string, string> = {
  outside_hours: "outside opening hours", holiday: "clinic holiday", doctor_off_shift: "doctor off shift",
};
