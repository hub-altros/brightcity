from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel

Platform = Literal["vk", "tg", "max"]


# --- auth ---

class AdminOut(BaseModel):
    id: int
    login: str
    display_name: str
    bot_platform: Optional[str]
    bot_user_id: Optional[int]


class LoginResp(BaseModel):
    redirect: str


class TwoFASetupResp(BaseModel):
    secret: str
    qr_data_url: str


class TwoFASetupConfirmResp(BaseModel):
    recovery_codes: list[str]


class OkResp(BaseModel):
    ok: bool = True
    messages: list[str] = []


# --- applications ---

class ApplicationListItem(BaseModel):
    id: int
    user_id: Optional[int]
    location: Optional[str]
    user_type: Optional[str]
    name_age: Optional[str]
    phone: Optional[str]
    status: str
    banner_url: Optional[str]
    created_at: datetime
    platform: Optional[str]
    first_media_id: Optional[int]


class ApplicationsListResp(BaseModel):
    items: list[ApplicationListItem]
    total: int
    page: int
    total_pages: int


class MediaItem(BaseModel):
    url: str
    media_id: Optional[int]


class HistoryRow(BaseModel):
    old_status: Optional[str]
    new_status: str
    admin_name: Optional[str]
    changed_at: datetime


class MessageRow(BaseModel):
    direction: str
    text: str
    created_at: datetime


class ApplicationDetailResp(BaseModel):
    id: int
    user_id: Optional[int]
    location: Optional[str]
    placement_location: Optional[str]
    user_type: Optional[str]
    phone: Optional[str]
    name_age: Optional[str]
    format_choice: Optional[str]
    training_info: Optional[str]
    status: str
    banner_url: Optional[str]
    banner_media_id: Optional[int]
    created_at: datetime
    platform: Optional[str]
    consent_version: Optional[str]
    rejection_reason: Optional[str]
    anonymized_at: Optional[datetime]
    child_photos: list[MediaItem]
    drawings: list[MediaItem]
    payment_docs: list[MediaItem]
    org_attachments: list[MediaItem]
    history: list[HistoryRow]
    messages: list[MessageRow]
    prev_id: Optional[int] = None
    next_id: Optional[int] = None


# --- gdpr ---

class GdprItem(BaseModel):
    id: int
    platform: str
    user_id: int
    requested_at: datetime


# --- stats ---

class StatsResp(BaseModel):
    total: int
    by_status: list[dict]
    by_city: list[dict]


# --- filters ---

class FilterOption(BaseModel):
    value: str
    label: str
    count: int = 0


class StatusOption(BaseModel):
    value: str
    label: str


class FiltersResp(BaseModel):
    cities: list[FilterOption]
    placement_locations: list[FilterOption]
    statuses: list[StatusOption]


# --- bans ---

class BanItem(BaseModel):
    platform: Platform
    user_id: int
    banned_by: Optional[int] = None
    banned_by_platform: Optional[str] = None
    banned_at: datetime
    expires_at: Optional[datetime] = None
    reason: Optional[str] = None
    ban_count: int = 0


class BanListResp(BaseModel):
    items: list[BanItem]
    total: int
    limit: int
    offset: int


class BanRequest(BaseModel):
    platform: Platform
    user_id: int
    reason: Optional[str] = ""


# --- incomplete applications ---

class IncompleteListItem(BaseModel):
    platform: Platform
    user_id: int
    state: str
    step_label: str
    updated_at: datetime
    name_age: Optional[str] = None
    phone: Optional[str] = None
    last_remind_at: Optional[datetime] = None


class IncompleteListResp(BaseModel):
    items: list[IncompleteListItem]
    total: int


class IncompleteDetailResp(BaseModel):
    platform: Platform
    user_id: int
    state: str
    step_label: str
    updated_at: datetime
    temp_data: dict


# --- chats (переписка) ---

class ChatListItem(BaseModel):
    platform: Platform
    user_id: int
    sender_name: Optional[str] = None
    sender_username: Optional[str] = None
    application_id: Optional[int] = None
    name_age: Optional[str] = None
    phone: Optional[str] = None
    application_status: Optional[str] = None
    last_message_at: datetime
    last_message_text: str
    last_message_direction: str
    is_unanswered: bool
    archived_at: Optional[datetime] = None


class ChatListResp(BaseModel):
    items: list[ChatListItem]
    total: int
    page: int
    total_pages: int


class QuoteRef(BaseModel):
    id: int
    text: str
    direction: str


class ChatMessage(BaseModel):
    id: int
    direction: str
    text: str
    created_at: datetime
    reply_to: Optional[QuoteRef] = None
    edited_at: Optional[datetime] = None


class ChatDetailResp(BaseModel):
    platform: Platform
    user_id: int
    messages: list[ChatMessage]
    application_id: Optional[int] = None
    name_age: Optional[str] = None
    phone: Optional[str] = None
    application_status: Optional[str] = None
    archived_at: Optional[datetime] = None


class ReplyResp(BaseModel):
    ok: bool = True
    bot_message_id: Optional[int] = None


class EditResp(BaseModel):
    ok: bool = True
    mode: Literal["native", "correction"] = "correction"