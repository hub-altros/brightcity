export type Admin = {
  id: number;
  login: string;
  display_name: string;
  bot_platform: string | null;
  bot_user_id: number | null;
};

export type ApplicationListItem = {
  id: number;
  user_id: number | null;
  location: string | null;
  user_type: string | null;
  name_age: string | null;
  phone: string | null;
  status: string;
  banner_url: string | null;
  created_at: string;
  platform: string | null;
  first_media_id: number | null;
};

export type ApplicationsListResp = {
  items: ApplicationListItem[];
  total: number;
  page: number;
  total_pages: number;
};

export type MediaItem = {
  url: string;
  media_id: number | null;
  open_url?: string | null;
  kind?: string | null;
};

export type HistoryRow = {
  old_status: string | null;
  new_status: string;
  admin_name: string | null;
  changed_at: string;
};

export type MessageRow = {
  direction: string;
  text: string;
  created_at: string;
};

export type IncompleteItem = {
  platform: string | null;
  user_id: number;
  state: string;
  step_label: string;
  updated_at: string;
  name_age: string | null;
  phone: string | null;
  last_remind_at: string | null;
};

export type ApplicationsIncompleteResp = {
  items: IncompleteItem[];
  total: number;
};

export type ApplicationDetail = {
  id: number;
  user_id: number | null;
  location: string | null;
  placement_location: string | null;
  user_type: string | null;
  phone: string | null;
  name_age: string | null;
  format_choice: string | null;
  training_info: string | null;
  status: string;
  banner_url: string | null;
  banner_media_id: number | null;
  banner_open_url?: string | null;
  created_at: string;
  platform: string | null;
  consent_version: string | null;
  rejection_reason: string | null;
  anonymized_at: string | null;
  child_photos: MediaItem[];
  drawings: MediaItem[];
  payment_docs: MediaItem[];
  org_attachments: MediaItem[];
  history: HistoryRow[];
  messages: MessageRow[];
  prev_id?: number | null;
  next_id?: number | null;
};

export type GdprItem = {
  id: number;
  platform: string;
  user_id: number;
  requested_at: string;
};

export type Stats = {
  total: number;
  by_status: { status: string; label: string; count: number }[];
  by_city: { city: string; count: number }[];
};

// --- chats (переписка) ---

export type ChatListItem = {
  platform: string;
  user_id: number;
  sender_name: string | null;
  sender_username: string | null;
  application_id: number | null;
  name_age: string | null;
  phone: string | null;
  application_status: string | null;
  last_message_at: string;
  last_message_text: string;
  last_message_direction: "in" | "out";
  is_unanswered: boolean;
  archived_at: string | null;
};

export type ChatListResp = {
  items: ChatListItem[];
  total: number;
  page: number;
  total_pages: number;
};

export type QuoteRef = {
  id: number;
  text: string;
  direction: "in" | "out";
};

export type ChatMessage = {
  id: number;
  direction: string;
  text: string;
  created_at: string;
  reply_to?: QuoteRef | null;
  edited_at?: string | null;
};

export type ChatDetail = {
  platform: string;
  user_id: number;
  messages: ChatMessage[];
  application_id: number | null;
  name_age: string | null;
  phone: string | null;
  application_status: string | null;
  archived_at: string | null;
};

export type ReplyResp = {
  ok: boolean;
  bot_message_id: number | null;
};

export type EditResp = {
  ok: boolean;
  mode: "native" | "correction";
};