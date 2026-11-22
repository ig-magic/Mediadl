"""
Telegram Profile, Channel Media, Story & Pinned-Highlights Service
==================================================================
1. Zero-Login Mode (Works Instantly Out-of-the-Box):
   - Fetches any public Telegram User / Channel / Group HD Profile Pic, Name, Bio,
     Subscribers/Members count, and Verified status via `https://t.me/<username>`.
   - Downloads HD Photos & Videos from any public Telegram Channel Post (`https://t.me/<ch>/<id>`)
     and lists recent media posts (`https://t.me/s/<ch>`).
2. Optional Telethon MTProto Mode (`/tglogin`):
   - Unlocks Telegram Active Stories (`GetPeerStoriesRequest`), Pinned Story Highlights
     (`GetPinnedStoriesRequest`), and exact Story links (`https://t.me/<user>/s/<id>`)!
"""

from __future__ import annotations

from dataclasses import dataclass, field
import html as htmllib
import io
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple
import requests

from .instagram_service import InstagramFetchError, MediaItem

logger = logging.getLogger(__name__)

TG_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)
TG_USER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")


class TelegramFetchError(InstagramFetchError):
    """User-friendly Telegram fetch error."""


@dataclass
class TgProfileResult:
    username: str
    title: str
    bio: str
    extra_stats: str  # e.g. "10 096 152 subscribers" or "4,500 members, 312 online"
    is_verified: bool
    is_channel_or_group: bool
    avatar_bytes: Optional[bytes] = None
    avatar_url: str = ""


@dataclass
class TgPostMedia:
    channel: str
    post_id: str
    caption: str
    views: str
    items: List[MediaItem] = field(default_factory=list)


@dataclass
class TgDownloadedStory:
    is_video: bool
    data: bytes
    caption: str = ""
    story_id: int = 0


def classify_telegram_input(raw_text: str) -> Tuple[str, str, str]:
    """
    Classify Telegram input into:
      - ('tg_story', username, story_id) -> t.me/<user>/s/<id>
      - ('tg_post', channel, post_id)    -> t.me/<channel>/<post_id>
      - ('tg_profile', username, '')     -> @user, user, t.me/<user>, t.me/s/<user>
    """
    text = (raw_text or "").strip()
    if text.startswith("/"):
        parts = text.split(maxsplit=1)
        text = parts[1].strip() if len(parts) > 1 else ""
    if not text:
        raise TelegramFetchError("Kripya Telegram username ya `t.me/...` link bhejein!")

    m_url = re.search(
        r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/([^\s?#]+)",
        text,
        re.I,
    )
    if m_url:
        segs = [s for s in m_url.group(1).strip("/").split("/") if s]
        if not segs:
            raise TelegramFetchError("Invalid Telegram link!")

        # t.me/s/<channel>[/<post_id>]
        if segs[0].lower() == "s" and len(segs) >= 2:
            ch = segs[1].lstrip("@")
            if len(segs) >= 3 and segs[2].isdigit():
                return ("tg_post", ch.lower(), segs[2])
            return ("tg_profile", ch.lower(), "")

        # t.me/<username>/s/<story_id>
        if len(segs) >= 3 and segs[1].lower() == "s" and segs[2].isdigit():
            return ("tg_story", segs[0].lstrip("@").lower(), segs[2])

        # t.me/<channel>/<post_id>
        if len(segs) >= 2 and segs[1].isdigit():
            return ("tg_post", segs[0].lstrip("@").lower(), segs[1])

        cand = segs[0].lstrip("@")
        if cand.startswith("+") or cand.lower() == "joinchat":
            raise TelegramFetchError("Private invite link se bina join kiye info nahi nikalti.")
        if TG_USER_RE.match(cand):
            return ("tg_profile", cand.lower(), "")

    cand = text.lstrip("@").strip().strip("/")
    if TG_USER_RE.match(cand):
        return ("tg_profile", cand.lower(), "")

    raise TelegramFetchError(
        "Invalid Telegram username ya link! Example: `/tg durov` ya `https://t.me/durov/340`"
    )


def _strip_html(raw_html: str) -> str:
    txt = re.sub(r"<br\s*/?>", "\n", raw_html, flags=re.I)
    txt = re.sub(r"<[^>]+>", "", txt)
    return htmllib.unescape(txt).strip()


class TelegramMediaService:
    """Zero-login Telegram Profile/Channel fetcher + optional Telethon MTProto Story engine."""

    import tempfile as _tempfile
    CONFIG_FILE = os.path.join(_tempfile.gettempdir(), "web_tg_mtproto.json")
    SESSION_PATH = os.path.join(_tempfile.gettempdir(), "web_tg_mtproto")

    def __init__(self) -> None:
        self.api_id: Optional[int] = None
        self.api_hash: str = ""
        self.logged_in_label: Optional[str] = None
        self._load_config()
        # Pending interactive /tglogin state
        self._pending_client: Any = None
        self._pending_phone: str = ""
        self._pending_phone_hash: str = ""
        self._pending_api_id: Optional[int] = None
        self._pending_api_hash: str = ""

    def _load_config(self) -> None:
        env_id = (os.getenv("TG_API_ID") or "").strip()
        env_hash = (os.getenv("TG_API_HASH") or "").strip()
        if env_id.isdigit() and env_hash:
            self.api_id = int(env_id)
            self.api_hash = env_hash
        if os.path.exists(self.CONFIG_FILE):
            try:
                with open(self.CONFIG_FILE, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                if str(cfg.get("api_id", "")).isdigit() and cfg.get("api_hash"):
                    self.api_id = int(cfg["api_id"])
                    self.api_hash = str(cfg["api_hash"])
                    self.logged_in_label = cfg.get("user_label")
            except Exception:
                pass

    def _save_config(self, api_id: int, api_hash: str, user_label: str) -> None:
        self.api_id = api_id
        self.api_hash = api_hash
        self.logged_in_label = user_label
        try:
            with open(self.CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(
                    {"api_id": api_id, "api_hash": api_hash, "user_label": user_label},
                    f,
                    indent=2,
                )
            os.chmod(self.CONFIG_FILE, 0o600)
        except Exception as exc:
            logger.warning("Could not write tg_mtproto.json: %s", exc)

    @property
    def has_story_mtproto(self) -> bool:
        return bool(
            self.api_id
            and self.api_hash
            and os.path.exists(self.SESSION_PATH + ".session")
        )

    # ------------------------------------------------------------------
    # Zero-Login Public Web Profile & Channel Post Downloader
    # ------------------------------------------------------------------

    def get_public_profile(self, username: str) -> TgProfileResult:
        clean = username.strip().lstrip("@").lower()
        resp = requests.get(
            f"https://t.me/{clean}",
            headers={"User-Agent": TG_UA, "Accept-Language": "en-US,en;q=0.9"},
            timeout=12,
        )
        if resp.status_code != 200:
            raise TelegramFetchError(f"Telegram `@{clean}` open nahi ho paya.")

        html = resp.text
        # If account doesn't exist, t.me shows generic "Telegram: Contact @..." without tgme_page_title
        title_m = re.search(r'class="tgme_page_title"[^>]*>\s*<span[^>]*>(.*?)</span>', html, re.S)
        if not title_m:
            raise TelegramFetchError(
                f"✈️ Telegram par `@{clean}` naam ka koi active public user/channel nahi mila!"
            )

        title = _strip_html(title_m.group(1))
        verified = 'class="verified-icon"' in html

        extra_m = re.search(r'class="tgme_page_extra"[^>]*>(.*?)</div>', html, re.S)
        extra = _strip_html(extra_m.group(1)) if extra_m else ""

        desc_m = re.search(r'class="tgme_page_description[^"]*"[^>]*>(.*?)</div>', html, re.S)
        bio = _strip_html(desc_m.group(1)) if desc_m else ""

        is_ch_or_grp = any(
            kw in extra.lower() for kw in ("subscriber", "member", "online")
        ) or ("Preview channel" in html)

        og_img_m = re.search(r'property="og:image"\s+content="([^"]+)"', html)
        img_url = htmllib.unescape(og_img_m.group(1)).strip() if og_img_m else ""
        avatar_bytes: Optional[bytes] = None
        if img_url.startswith("http") and "telegram.org/img/t_logo" not in img_url:
            try:
                ir = requests.get(img_url, headers={"User-Agent": TG_UA}, timeout=12)
                if ir.status_code == 200 and len(ir.content) > 500:
                    avatar_bytes = ir.content
            except Exception:
                avatar_bytes = None

        return TgProfileResult(
            username=clean,
            title=title,
            bio=bio,
            extra_stats=extra,
            is_verified=verified,
            is_channel_or_group=is_ch_or_grp,
            avatar_bytes=avatar_bytes,
            avatar_url=img_url,
        )

    def download_channel_post(self, channel: str, post_id: str) -> TgPostMedia:
        clean = channel.strip().lstrip("@").lower()
        resp = requests.get(
            f"https://t.me/{clean}/{post_id}?embed=1&mode=tme",
            headers={"User-Agent": TG_UA, "Referer": f"https://t.me/{clean}/{post_id}"},
            timeout=15,
        )
        if resp.status_code != 200 or "tgme_widget_message_error" in resp.text:
            raise TelegramFetchError(f"Post `t.me/{clean}/{post_id}` nahi mili ya private group mein hai.")

        html = resp.text
        cap_m = re.search(r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', html, re.S)
        caption = _strip_html(cap_m.group(1)) if cap_m else ""

        views_m = re.search(r'class="tgme_widget_message_views"[^>]*>(.*?)</span>', html, re.S)
        views = _strip_html(views_m.group(1)) if views_m else ""

        items: List[MediaItem] = []
        seen_urls = set()

        # Videos (<video src="...">)
        for v_url in re.findall(r'<video[^>]+src="([^"]+)"', html):
            u = htmllib.unescape(v_url).strip()
            if u.startswith("http") and u not in seen_urls:
                seen_urls.add(u)
                items.append(MediaItem(is_video=True, media_url=u, shortcode=post_id))

        # Photos (tgme_widget_message_photo_wrap background-image:url('...'))
        for p_m in re.finditer(
            r'tgme_widget_message_photo_wrap[^>]+background-image:url\(\'([^\']+)\'\)',
            html,
        ):
            u = htmllib.unescape(p_m.group(1)).strip()
            if u.startswith("http") and u not in seen_urls:
                seen_urls.add(u)
                items.append(MediaItem(is_video=False, media_url=u, shortcode=post_id))

        if not items:
            raise TelegramFetchError(
                f"`t.me/{clean}/{post_id}` mein koi downloadable photo/video nahi hai (ya file >50MB web preview ke bahar hai)."
            )

        return TgPostMedia(
            channel=clean,
            post_id=post_id,
            caption=caption,
            views=views,
            items=items,
        )

    def get_recent_channel_posts(self, channel: str, limit: int = 8) -> List[Dict[str, str]]:
        """List recent media posts from `https://t.me/s/<channel>`."""
        clean = channel.strip().lstrip("@").lower()
        resp = requests.get(
            f"https://t.me/s/{clean}",
            headers={"User-Agent": TG_UA},
            timeout=15,
        )
        if resp.status_code != 200:
            return []

        blocks = resp.text.split('data-post="')
        found: List[Dict[str, str]] = []
        for blk in reversed(blocks[1:]):
            post_ref = blk.split('"', 1)[0]  # e.g. "durov/340"
            if "/" not in post_ref:
                continue
            pid = post_ref.split("/", 1)[1]
            has_vid = "<video" in blk or "tgme_widget_message_video" in blk
            has_pic = "tgme_widget_message_photo_wrap" in blk
            if not (has_vid or has_pic):
                continue
            cap_m = re.search(r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', blk, re.S)
            snip = _strip_html(cap_m.group(1))[:40] if cap_m else ""
            found.append(
                {
                    "post_id": pid,
                    "icon": "🎬" if has_vid else "🖼",
                    "snippet": snip or f"Post #{pid}",
                }
            )
            if len(found) >= limit:
                break
        return found

    # ------------------------------------------------------------------
    # Optional Telethon MTProto Setup & Story / Highlight Downloader
    # ------------------------------------------------------------------

    async def start_mtproto_login(self, api_id: int, api_hash: str, phone: str) -> None:
        from telethon import TelegramClient

        if self._pending_client:
            try:
                await self._pending_client.disconnect()
            except Exception:
                pass

        client = TelegramClient(self.SESSION_PATH, api_id, api_hash)
        await client.connect()
        sent = await client.send_code_request(phone)
        self._pending_client = client
        self._pending_phone = phone
        self._pending_phone_hash = sent.phone_code_hash
        self._pending_api_id = api_id
        self._pending_api_hash = api_hash

    async def verify_mtproto_otp(self, code: str) -> Tuple[str, str]:
        """Returns ('ok', user_label) or ('need_2fa', '')."""
        from telethon.errors import SessionPasswordNeededError

        if not self._pending_client:
            raise TelegramFetchError("Koi active `/tglogin` process nahi mila. Dobara `/tglogin` karein.")
        try:
            me = await self._pending_client.sign_in(
                phone=self._pending_phone,
                code=code.strip().replace(" ", "").replace("-", ""),
                phone_code_hash=self._pending_phone_hash,
            )
        except SessionPasswordNeededError:
            return ("need_2fa", "")
        except Exception as exc:
            raise TelegramFetchError(f"OTP verify nahi ho paya: {exc}") from exc

        label = f"@{me.username}" if getattr(me, "username", None) else str(me.first_name or me.id)
        assert self._pending_api_id is not None
        self._save_config(self._pending_api_id, self._pending_api_hash, label)
        await self._pending_client.disconnect()
        self._pending_client = None
        return ("ok", label)

    async def verify_mtproto_2fa(self, password: str) -> str:
        if not self._pending_client:
            raise TelegramFetchError("Koi active `/tglogin` process nahi mila.")
        try:
            me = await self._pending_client.sign_in(password=password)
        except Exception as exc:
            raise TelegramFetchError(f"2FA password galat ya error: {exc}") from exc

        label = f"@{me.username}" if getattr(me, "username", None) else str(me.first_name or me.id)
        assert self._pending_api_id is not None
        self._save_config(self._pending_api_id, self._pending_api_hash, label)
        await self._pending_client.disconnect()
        self._pending_client = None
        return label

    async def fetch_stories_mtproto(
        self,
        username: str,
        pinned: bool = False,
        story_id: Optional[int] = None,
        limit: int = 10,
    ) -> List[TgDownloadedStory]:
        """Download active Telegram Stories or Pinned Highlights via Telethon MTProto."""
        if not self.has_story_mtproto or not self.api_id:
            raise TelegramFetchError(
                "Telegram Stories download karne ke liye pehle `/tglogin` se MTProto connect karein!"
            )

        from telethon import TelegramClient
        from telethon.tl.functions import stories as tl_stories
        from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto

        clean = username.strip().lstrip("@")
        out: List[TgDownloadedStory] = []
        client = TelegramClient(self.SESSION_PATH, self.api_id, self.api_hash)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise TelegramFetchError("Telegram MTProto session expire ho gaya. `/tglogin` dobara karein.")

            entity = await client.get_entity(clean)
            if story_id is not None:
                res = await client(tl_stories.GetStoriesByIDRequest(peer=entity, id=[story_id]))
                story_items = getattr(res, "stories", []) or []
            elif pinned:
                res = await client(
                    tl_stories.GetPinnedStoriesRequest(peer=entity, offset_id=0, limit=limit)
                )
                story_items = getattr(res, "stories", []) or []
            else:
                res = await client(tl_stories.GetPeerStoriesRequest(peer=entity))
                peer_stories = getattr(res, "stories", None)
                story_items = getattr(peer_stories, "stories", []) if peer_stories else []

            for st in story_items[:limit]:
                media = getattr(st, "media", None)
                if not media:
                    continue
                is_vid = isinstance(media, MessageMediaDocument)
                if not (is_vid or isinstance(media, MessageMediaPhoto)):
                    continue
                buf = io.BytesIO()
                await client.download_media(media, file=buf)
                raw = buf.getvalue()
                if len(raw) > 400:
                    out.append(
                        TgDownloadedStory(
                            is_video=is_vid,
                            data=raw,
                            caption=str(getattr(st, "caption", "") or ""),
                            story_id=int(getattr(st, "id", 0) or 0),
                        )
                    )
        finally:
            await client.disconnect()

        return out
