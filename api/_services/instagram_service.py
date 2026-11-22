"""
Instagram Profile Picture Fetcher Service (Step 1: HD Profile Pic Only)
=======================================================================
Multi-layer 2026 Instagram fetcher designed to work:
  1. Directly without login on home/mobile/ISP networks (Embedded Relay Page JSON)
  2. Directly without login EVEN on cloud/VPS datacenter IPs (GraphQL Post-Bridge +
     PolarisProfilePageContentQuery with auto-refreshed doc_ids)
  3. With optional IG_SESSIONID cookie (unlocks private/zero-post accounts on cloud IPs)
  4. Built-in Step-2 readiness (_raw_profile stores followers/following/bio for next step)
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)
IG_WEB_APP_ID = "936619743392459"

RESERVED_PATHS = {
    "p", "reel", "reels", "tv", "stories", "explore", "accounts",
    "direct", "about", "legal", "developer", "directory", "web",
    "api", "graphql", "challenge", "privacy", "terms",
}

USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")


class InstagramFetchError(Exception):
    """Base error for user-friendly bot error reporting."""


class InvalidUsernameError(InstagramFetchError):
    """Raised when input is not a valid Instagram username or link."""


class ProfileNotFoundError(InstagramFetchError):
    """Raised when the Instagram account does not exist or cannot be resolved."""

    def __init__(self, message: str, suggestions: Optional[List[str]] = None) -> None:
        super().__init__(message)
        self.suggestions: List[str] = suggestions or []


class InstagramLoginError(InstagramFetchError):
    """Raised when Instagram username + password login fails."""


class TwoFactorRequiredError(InstagramLoginError):
    """Raised when Instagram requires a 6-digit 2FA OTP code to finish login."""


@dataclass
class MediaItem:
    """Single photo or video item inside a Story, Post, Reel, Carousel, or Highlight."""
    is_video: bool
    media_url: str
    thumbnail_url: str = ""
    shortcode: str = ""
    taken_at: Optional[int] = None


@dataclass
class PostDownloadResult:
    """Full Instagram Post / Reel / Carousel result."""
    shortcode: str
    owner_username: str
    owner_full_name: str
    caption: str
    like_count: Optional[int]
    comment_count: Optional[int]
    view_count: Optional[int]
    taken_at: Optional[int]
    items: List[MediaItem] = field(default_factory=list)


@dataclass
class PostSummary:
    """Summary card for browsing a user's recent Posts or Reels."""
    shortcode: str
    is_video: bool
    is_carousel: bool
    caption: str = ""
    like_count: Optional[int] = None
    view_count: Optional[int] = None
    taken_at: Optional[int] = None


@dataclass
class HighlightSummary:
    """Summary item for a user's Story Highlight tray."""
    highlight_id: str
    title: str


@dataclass
class ProfilePicResult:
    """Full Profile Info + HD Profile Picture."""
    username: str
    user_id: str
    hd_pic_url: str
    image_bytes: bytes
    method_used: str
    _raw_profile: Dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def full_name(self) -> str:
        return str(self._raw_profile.get("full_name") or "").strip()

    @property
    def biography(self) -> str:
        return str(self._raw_profile.get("biography") or "").strip()

    @property
    def external_url(self) -> str:
        ext = str(self._raw_profile.get("external_url") or "").strip()
        if ext:
            return ext
        links = self._raw_profile.get("bio_links")
        if isinstance(links, list) and links:
            first = links[0]
            if isinstance(first, dict) and first.get("url"):
                return str(first["url"]).strip()
        return ""

    @property
    def category(self) -> str:
        return str(
            self._raw_profile.get("category_name")
            or self._raw_profile.get("category")
            or ""
        ).strip()

    @property
    def followers_count(self) -> Optional[int]:
        p = self._raw_profile
        val = p.get("follower_count")
        if val is None and isinstance(p.get("edge_followed_by"), dict):
            val = p["edge_followed_by"].get("count")
        return int(val) if isinstance(val, (int, float)) else None

    @property
    def following_count(self) -> Optional[int]:
        p = self._raw_profile
        val = p.get("following_count")
        if val is None and isinstance(p.get("edge_follow"), dict):
            val = p["edge_follow"].get("count")
        return int(val) if isinstance(val, (int, float)) else None

    @property
    def posts_count(self) -> Optional[int]:
        p = self._raw_profile
        val = p.get("media_count")
        if val is None and isinstance(p.get("edge_owner_to_timeline_media"), dict):
            val = p["edge_owner_to_timeline_media"].get("count")
        if val is None:
            val = p.get("posts_count")
        return int(val) if isinstance(val, (int, float)) else None

    @property
    def is_private(self) -> bool:
        return bool(self._raw_profile.get("is_private"))

    @property
    def is_verified(self) -> bool:
        return bool(self._raw_profile.get("is_verified"))

    @property
    def has_public_story(self) -> bool:
        return bool(
            self._raw_profile.get("has_public_story")
            or self._raw_profile.get("latest_reel_media")
        )


def classify_instagram_input(raw_text: str) -> Tuple[str, str, str]:
    """
    Classify any user message/link into:
      - ('post', shortcode, '')      -> /p/..., /reel/..., /tv/..., or /<user>/reel/...
      - ('story', username, story_id)-> /stories/<username>/[<story_id>]
      - ('profile', username, '')    -> @username, username, or profile URL
    """
    text = (raw_text or "").strip()
    if not text:
        raise InvalidUsernameError("Kripya koi Instagram username, story link, ya post/reel link bhejein.")

    if text.startswith("/"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            raise InvalidUsernameError("Command ke saath username ya link likhein.")
        text = parts[1].strip()

    url_match = re.search(
        r"(?:https?://)?(?:www\.|m\.)?instagram\.com/([^\s?#]+)",
        text,
        re.IGNORECASE,
    )
    if url_match:
        path = url_match.group(1).strip("/")
        segments = [s for s in path.split("/") if s]
        if not segments:
            raise InvalidUsernameError("Ye valid Instagram link nahi hai.")

        first = segments[0].lower()
        # Direct /p/<code>, /reel/<code>, /reels/<code>, /tv/<code>
        if first in {"p", "reel", "reels", "tv"} and len(segments) >= 2:
            shortcode = segments[1]
            if re.match(r"^[A-Za-z0-9_-]{5,35}$", shortcode):
                return ("post", shortcode, "")
            raise InvalidUsernameError("Instagram post/reel link sahi nahi lag raha.")

        # /<username>/p/<code> or /<username>/reel/<code>
        if len(segments) >= 3 and segments[1].lower() in {"p", "reel", "reels", "tv"}:
            shortcode = segments[2]
            if re.match(r"^[A-Za-z0-9_-]{5,35}$", shortcode):
                return ("post", shortcode, segments[0].lstrip("@").lower())

        # Story link: /stories/<username>/[<story_id>]
        if first == "stories" and len(segments) >= 2:
            second = segments[1].lstrip("@")
            if second.lower() == "highlights" and len(segments) >= 3:
                return ("highlight", "", re.sub(r"\D", "", segments[2]))
            if _is_valid_username(second):
                story_id = re.sub(r"\D", "", segments[2]) if len(segments) >= 3 else ""
                return ("story", second.lower(), story_id)

        if first in RESERVED_PATHS:
            raise InvalidUsernameError(
                "Ye Instagram system link hai. Kisi user ka profile, post, reel ya story link bhejein."
            )

        candidate = segments[0].lstrip("@")
        if _is_valid_username(candidate):
            return ("profile", candidate.lower(), "")
        raise InvalidUsernameError(f"`{candidate}` valid Instagram username nahi hai.")

    candidate = text.lstrip("@").strip().strip("/")
    if _is_valid_username(candidate):
        return ("profile", candidate.lower(), "")

    raise InvalidUsernameError(
        "Invalid Instagram input! Username (`virat.kohli`) ya Post/Reel/Story link bhejein."
    )


def parse_instagram_input(raw_text: str) -> Tuple[str, str]:
    """
    Parse user message into ('username', username) or ('shortcode', shortcode) for profile/DP lookup.
    """
    kind, val1, _ = classify_instagram_input(raw_text)
    if kind == "post":
        return ("shortcode", val1)
    if kind in {"profile", "story"} and val1:
        return ("username", val1)
    raise InvalidUsernameError(
        "Invalid Instagram username! Example: `virat.kohli` ya `https://www.instagram.com/virat.kohli`"
    )


def _is_valid_username(username: str) -> bool:
    if not username or not USERNAME_RE.match(username):
        return False
    if username.startswith(".") or username.endswith(".") or ".." in username:
        return False
    return username.lower() not in RESERVED_PATHS


def _extract_embedded_relay_data(obj: Any) -> Iterator[Dict[str, Any]]:
    """Recursively yield Relay `__bbox.result.data` objects embedded in Instagram HTML."""
    if isinstance(obj, dict):
        bbox = obj.get("__bbox")
        if isinstance(bbox, dict):
            result = bbox.get("result")
            if isinstance(result, dict) and isinstance(result.get("data"), dict):
                yield result["data"]
        for val in obj.values():
            yield from _extract_embedded_relay_data(val)
    elif isinstance(obj, list):
        for item in obj:
            yield from _extract_embedded_relay_data(item)


def _extract_best_hd_pic_url(user_dict: Dict[str, Any]) -> Optional[str]:
    """Pick the highest-resolution profile picture URL available in user_dict."""
    hd_info = user_dict.get("hd_profile_pic_url_info")
    if isinstance(hd_info, dict) and hd_info.get("url"):
        return hd_info["url"]

    hd_versions = user_dict.get("hd_profile_pic_versions")
    if isinstance(hd_versions, list) and hd_versions:
        # Sort by width descending if width present
        best = max(hd_versions, key=lambda x: x.get("width", 0) if isinstance(x, dict) else 0)
        if isinstance(best, dict) and best.get("url"):
            return best["url"]

    for key in ("profile_pic_url_hd", "profile_pic_url"):
        val = user_dict.get(key)
        if isinstance(val, str) and val.startswith("http"):
            return val
    return None


class InstagramService:
    """
    Fetches HD Instagram Profile Pictures reliably across residential & cloud IPs.
    """

    # Current 2026 Relay doc_ids (with automatic live JS refresh fallback)
    DEFAULT_DOC_IDS = {
        "profile_by_id": ["27937681195819736", "40158838120366999", "26672929172408668"],
        "post_by_shortcode": ["27128499623469141"],
        "search_users": ["27634848489527274"],
        "auth_search_container": ["28034859679513798", "27706427925724183"],
    }

    BROWSER_UAS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    ]

    import tempfile as _tempfile
    SESSION_FILE = os.path.join(_tempfile.gettempdir(), "web_ig_session.json")

    def __init__(self, session_id: Optional[str] = None, cache_ttl: int = 300) -> None:
        self.session_id = urllib.parse.unquote((session_id or os.getenv("IG_SESSIONID") or "").strip())
        self.logged_in_user: Optional[str] = None
        self._extra_cookies: Dict[str, str] = {}
        self._auth_session: Optional[requests.Session] = None
        self._anon_ig_cooldown_until: float = 0.0
        self._ua_idx: int = 0
        # Cached StoryNavigation session + CSRF for unlimited Private/Public profile resolution
        self._storynav_session: Optional[requests.Session] = None
        self._storynav_csrf: str = ""
        self._storynav_ts: float = 0.0
        self.cache_ttl = cache_ttl
        self._cache: Dict[str, Tuple[float, ProfilePicResult]] = {}
        # (ts, csrf, lsd, fb_dtsg, av)
        self._tokens_cache: Optional[Tuple[float, str, str, str, str]] = None
        self._live_doc_ids_refreshed = False
        self._pending_2fa_loader: Any = None
        self._pending_2fa_user: Optional[str] = None
        self._load_saved_session()

    def _next_ua(self) -> str:
        ua = self.BROWSER_UAS[self._ua_idx % len(self.BROWSER_UAS)]
        self._ua_idx += 1
        return ua

    def _load_saved_session(self) -> None:
        if not os.path.exists(self.SESSION_FILE):
            return
        try:
            with open(self.SESSION_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            cookies = saved.get("cookies") or {}
            if cookies.get("sessionid"):
                self._extra_cookies = {str(k): str(v) for k, v in cookies.items() if v}
                self.session_id = urllib.parse.unquote(str(cookies["sessionid"]))
                self.logged_in_user = saved.get("username") or None
                logger.info("🔐 Loaded saved Instagram session for @%s", self.logged_in_user or "cookie_user")
        except Exception as exc:
            logger.debug("Could not load ig_session.json: %s", exc)

    def _save_session_file(self, username: str, cookies: Dict[str, str]) -> None:
        try:
            with open(self.SESSION_FILE, "w", encoding="utf-8") as f:
                json.dump({"username": username, "cookies": cookies}, f, indent=2)
            os.chmod(self.SESSION_FILE, 0o600)
        except Exception as exc:
            logger.warning("Could not write ig_session.json: %s", exc)

    def set_session_id(self, session_id: str) -> None:
        """Update Instagram sessionid cookie at runtime (unlocks private/0-post accounts on cloud IPs)."""
        clean = urllib.parse.unquote((session_id or "").strip())
        self.session_id = clean
        self._extra_cookies["sessionid"] = clean
        self.logged_in_user = self.logged_in_user or "cookie_session"
        self._auth_session = None
        self._tokens_cache = None
        self._save_session_file(self.logged_in_user, self._extra_cookies)

    def login_with_password(self, ig_username: str, ig_password: str) -> str:
        """
        Log in to Instagram using Username (ID) + Password via Instaloader's browser login flow,
        extract the authenticated cookie jar (`sessionid`, `csrftoken`, `ds_user_id`, `mid`),
        and persist to `ig_session.json`.
        """
        import instaloader

        clean_user = ig_username.strip().lstrip("@")
        if not clean_user or not ig_password:
            raise InstagramLoginError("Username aur Password dono zaroori hain.")

        loader = instaloader.Instaloader(
            download_pictures=False,
            download_videos=False,
            download_video_thumbnails=False,
            save_metadata=False,
            quiet=True,
        )
        try:
            loader.login(clean_user, ig_password)
        except instaloader.TwoFactorAuthRequiredException as exc:
            self._pending_2fa_loader = loader
            self._pending_2fa_user = clean_user
            raise TwoFactorRequiredError(
                f"@{clean_user} par 2-Factor Authentication (OTP) on hai! "
                "Apne SMS / Authenticator app mein aaya 6-digit OTP code bhejein."
            ) from exc
        except instaloader.BadCredentialsException as exc:
            raise InstagramLoginError(
                "❌ Instagram ID (username) ya Password galat hai! Check karke dobara `/login` karein."
            ) from exc
        except instaloader.ConnectionException as exc:
            msg = str(exc)
            if "Checkpoint" in msg or "checkpoint" in msg.lower():
                raise InstagramLoginError(
                    "⚠️ Instagram Security Approval chahiye!\n\n"
                    "1️⃣ Apna Instagram Mobile App open karein\n"
                    "2️⃣ Notifications / Settings → Security → Login Activity mein **'This was me'** approve karein\n"
                    "3️⃣ Phir 10 second baad dobara `/login` karein.\n"
                    "*(Tip: Bot ke liye secondary/extra Instagram ID use karna best rehta hai)*"
                ) from exc
            raise InstagramLoginError(f"⚠️ Instagram connection error: {msg}") from exc
        except instaloader.LoginException as exc:
            msg = str(exc)
            if "Checkpoint" in msg or "checkpoint" in msg.lower():
                raise InstagramLoginError(
                    "⚠️ Instagram Security Approval chahiye!\n\n"
                    "Apna Instagram App kholkar **'This was me'** approve karein, "
                    "aur phir 10 second baad dobara `/login` karein."
                ) from exc
            raise InstagramLoginError(f"❌ Login nahi ho paya: {msg}") from exc

        return self._finalize_instaloader_login(loader, clean_user)

    def complete_2fa_login(self, otp_code: str) -> str:
        """Complete pending 2FA login using the 6-digit OTP code."""
        import instaloader

        if not self._pending_2fa_loader or not self._pending_2fa_user:
            raise InstagramLoginError("Koi 2FA login pending nahi hai. Pehle `/login` karein.")

        code = re.sub(r"\s+", "", otp_code or "")
        loader = self._pending_2fa_loader
        user = self._pending_2fa_user
        try:
            loader.two_factor_login(code)
        except instaloader.BadCredentialsException as exc:
            raise InstagramLoginError("❌ OTP code galat hai! Sahi 6-digit code bhejein.") from exc
        except Exception as exc:
            raise InstagramLoginError(f"❌ 2FA verify nahi ho paya: {exc}") from exc

        self._pending_2fa_loader = None
        self._pending_2fa_user = None
        return self._finalize_instaloader_login(loader, user)

    def _finalize_instaloader_login(self, loader: Any, username: str) -> str:
        cookies = {
            str(k): str(v)
            for k, v in requests.utils.dict_from_cookiejar(loader.context._session.cookies).items()
            if v
        }
        sess_id = urllib.parse.unquote(cookies.get("sessionid", ""))
        if not sess_id:
            raise InstagramLoginError("Login ke baad sessionid nahi mila. Instagram app mein security prompt check karein.")

        self._extra_cookies = cookies
        self.session_id = sess_id
        self.logged_in_user = loader.context.username or username
        self._auth_session = None
        self._tokens_cache = None
        self._cache.clear()
        self._save_session_file(self.logged_in_user, cookies)
        return self.logged_in_user

    def logout(self) -> None:
        """Clear active Instagram login & saved session file."""
        self.session_id = ""
        self.logged_in_user = None
        self._extra_cookies = {}
        self._auth_session = None
        self._tokens_cache = None
        self._pending_2fa_loader = None
        self._pending_2fa_user = None
        if os.path.exists(self.SESSION_FILE):
            try:
                os.remove(self.SESSION_FILE)
            except Exception:
                pass

    def _build_session(self) -> requests.Session:
        # When logged in, reuse a persistent cookie jar so Instagram's rotated
        # Set-Cookie values (csrftoken, rur, shbid) are preserved across searches!
        if self.session_id:
            if self._auth_session is None:
                sess = requests.Session()
                sess.headers.update({
                    "User-Agent": DEFAULT_UA,
                    "Accept-Language": "en-US,en;q=0.9",
                })
                sess.cookies.set("ig_pr", "1", domain=".instagram.com")
                sess.cookies.set("ig_vw", "1920", domain=".instagram.com")
                for k, v in self._extra_cookies.items():
                    if v:
                        sess.cookies.set(k, v, domain=".instagram.com")
                sess.cookies.set("sessionid", self.session_id, domain=".instagram.com")
                self._auth_session = sess
            return self._auth_session

        sess = requests.Session()
        sess.headers.update({
            "User-Agent": self._next_ua(),
            "Accept-Language": "en-US,en;q=0.9",
        })
        sess.cookies.set("ig_pr", "1", domain=".instagram.com")
        sess.cookies.set("ig_vw", "1920", domain=".instagram.com")
        return sess

    def get_profile_pic(self, raw_input: str) -> ProfilePicResult:
        """Main entry point: accepts username, @username, profile URL, or post/reel URL."""
        kind, value = parse_instagram_input(raw_input)
        cache_key = f"{kind}:{value.lower()}"

        now = time.time()
        cached = self._cache.get(cache_key)
        if cached and (now - cached[0]) < self.cache_ttl:
            return cached[1]

        if kind == "shortcode":
            user_data, method = self._resolve_from_shortcode(value)
        else:
            user_data, method = self._resolve_from_username(value)

        username = (user_data.get("username") or value).lower()
        user_id = str(user_data.get("pk") or user_data.get("id") or "")
        hd_url = _extract_best_hd_pic_url(user_data)
        if not hd_url:
            raise ProfileNotFoundError(
                f"@{username} ki profile picture URL nahi mil payi."
            )

        image_bytes = self._download_image_bytes(hd_url)
        result = ProfilePicResult(
            username=username,
            user_id=user_id,
            hd_pic_url=hd_url,
            image_bytes=image_bytes,
            method_used=method,
            _raw_profile=user_data,
        )

        # Store under both input cache_key and canonical username
        self._cache[cache_key] = (now, result)
        self._cache[f"username:{username}"] = (now, result)
        if len(self._cache) > 100:
            oldest = min(self._cache, key=lambda k: self._cache[k][0])
            self._cache.pop(oldest, None)

        return result

    def get_cached_by_username(self, username: str) -> Optional[ProfilePicResult]:
        """Fast lookup for inline button callbacks (e.g. uncompressed HD document download)."""
        key = f"username:{username.lower().lstrip('@')}"
        cached = self._cache.get(key)
        if cached and (time.time() - cached[0]) < self.cache_ttl:
            return cached[1]
        return None

    # ------------------------------------------------------------------
    # Resolution Strategies
    # ------------------------------------------------------------------

    def _resolve_from_username(self, username: str) -> Tuple[Dict[str, Any], str]:
        now = time.time()

        # Path A: Logged-in session -> use lightweight JSON APIs FIRST (avoids HTML-scrape rate limits!)
        if self.session_id:
            try:
                user = self._layer2_web_profile_info(username)
                if user:
                    return user, "web_profile_info_auth"
            except ProfileNotFoundError:
                raise
            except Exception as exc:
                logger.debug("Auth Layer 2 skipped for %s: %s", username, exc)

            try:
                uid = self._search_user_id_graphql(username)
                if uid:
                    user = self._fetch_profile_by_id_graphql(uid)
                    if user and user.get("username", "").lower() == username.lower():
                        return user, "graphql_auth_search"
            except Exception as exc:
                logger.debug("Auth Layer 3a skipped for %s: %s", username, exc)

            try:
                user = self._layer1_profile_page(username)
                if user:
                    return user, "profile_page_auth"
            except ProfileNotFoundError:
                raise
            except Exception as exc:
                logger.debug("Auth Layer 1 skipped for %s: %s", username, exc)

        # Path B: Anonymous direct Instagram (with smart 3-min cooldown once IG blocks anonymous IP after 2-3 hits)
        elif now >= self._anon_ig_cooldown_until:
            try:
                user = self._layer1_profile_page(username)
                if user:
                    return user, "profile_page_relay"
            except ProfileNotFoundError:
                raise
            except Exception as exc:
                logger.debug("Anon Layer 1 skipped for %s: %s", username, exc)

            if time.time() >= self._anon_ig_cooldown_until:
                try:
                    user = self._layer2_web_profile_info(username)
                    if user:
                        return user, "web_profile_info"
                except ProfileNotFoundError:
                    raise
                except Exception as exc:
                    logger.debug("Anon Layer 2 skipped for %s: %s", username, exc)

        # Layer 3a (NEW): Private-Capable Unlimited Resolver (StoryNavigation + 1080p GraphQL Upgrade!)
        # Works WITHOUT login even on Private & 0-Post accounts without 2-3 search limits!
        try:
            user = self._layer3_storynav_private_capable(username)
            if user:
                return user, "storynav_private_capable"
        except Exception as exc:
            logger.debug("Layer 3a (storynav) skipped for %s: %s", username, exc)

        # Layer 3b: Fresh-Session Residential Proxy Resolver + 1080p GraphQL Upgrade
        try:
            user = self._layer3_residential_proxy_api(username)
            if user:
                return user, "residential_proxy_api"
        except Exception as exc:
            logger.debug("Layer 3b (residential proxy api) skipped for %s: %s", username, exc)

        # Layer 3c (NEW): Exact-Match Search Avatar Resolver (catches unindexed Private accounts!)
        try:
            user = self._layer3_exact_search_fallback(username)
            if user:
                return user, "exact_search_fallback"
        except Exception as exc:
            logger.debug("Layer 3c (exact search fallback) skipped for %s: %s", username, exc)

        # Layer 3d: Zero-Login Cloud-IP Shortcode Bridge -> PolarisProfilePageContentQuery
        try:
            uid = self._resolve_user_id_via_search_bridge(username)
            if uid:
                user = self._fetch_profile_by_id_graphql(uid)
                if user and user.get("username", "").lower() == username.lower():
                    return user, "graphql_cloud_bridge"
        except Exception as exc:
            logger.debug("Layer 3d (cloud bridge) skipped for %s: %s", username, exc)

        # Layer 3e: Public HD mirror fallback
        try:
            user = self._layer3c_public_mirror(username)
            if user:
                return user, "public_hd_mirror"
        except Exception as exc:
            logger.debug("Layer 3e (public mirror) skipped for %s: %s", username, exc)

        suggestions = self.search_similar_usernames(username)
        raise ProfileNotFoundError(
            f"@{username} naam ka koi active Instagram account nahi mila! Spelling check karein.",
            suggestions=suggestions,
        )

    def _resolve_from_shortcode(self, shortcode: str) -> Tuple[Dict[str, Any], str]:
        owner_id = self._get_owner_id_from_shortcode(shortcode)
        if owner_id:
            user = self._fetch_profile_by_id_graphql(owner_id)
            if user:
                return user, "graphql_shortcode"
        # Fallback via download_post_or_reel -> owner username
        try:
            post = self.download_post_or_reel(shortcode)
            if post.owner_username:
                return self._resolve_from_username(post.owner_username)
        except Exception:
            pass
        raise ProfileNotFoundError("Is post/reel link se Instagram account nahi mil paya.")

    # ------------------------------------------------------------------
    # Layer Implementations
    # ------------------------------------------------------------------

    def _layer1_profile_page(self, username: str) -> Optional[Dict[str, Any]]:
        sess = self._build_session()
        headers = {
            "Accept": "text/html",
            "Referer": "https://www.instagram.com/",
            "x-ig-app-id": IG_WEB_APP_ID,
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
        }
        resp = sess.get(
            f"https://www.instagram.com/{username}/",
            headers=headers,
            allow_redirects=False,
            timeout=10,
        )
        if resp.status_code == 404:
            raise ProfileNotFoundError(f"@{username} naam ka koi Instagram account mojood nahi hai.")
        if resp.status_code in (301, 302, 401, 429):
            if not self.session_id:
                self._anon_ig_cooldown_until = time.time() + 180
            return None
        if resp.status_code != 200:
            return None

        user: Dict[str, Any] = {}
        for match in re.finditer(
            r'<script type="application/json"[^>]*>(.*?)</script>',
            resp.text,
            re.DOTALL,
        ):
            try:
                blob = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
            for data in _extract_embedded_relay_data(blob):
                if isinstance(data.get("xig_user_by_username"), dict):
                    user.update(data["xig_user_by_username"])

        if user.get("pk") and user.get("username", "").lower() == username.lower():
            return user

        # Check if Instagram rendered a 200 "Sorry, this page isn't available" shell
        if '"httpErrorPage"' in resp.text:
            raise ProfileNotFoundError(f"@{username} naam ka koi Instagram account mojood nahi hai.")
        return None

    def _layer2_web_profile_info(self, username: str) -> Optional[Dict[str, Any]]:
        sess = self._build_session()
        csrf, lsd, _, _ = self._get_web_tokens()
        headers = {
            "Accept": "*/*",
            "Referer": f"https://www.instagram.com/{username}/",
            "x-ig-app-id": IG_WEB_APP_ID,
            "x-csrftoken": csrf,
            "x-fb-lsd": lsd,
            "x-requested-with": "XMLHttpRequest",
        }
        resp = sess.get(
            f"https://www.instagram.com/api/v1/users/web_profile_info/?username={username}",
            headers=headers,
            timeout=10,
        )
        if resp.status_code == 404:
            raise ProfileNotFoundError(f"@{username} naam ka koi Instagram account mojood nahi hai.")
        if resp.status_code in (401, 429):
            if not self.session_id:
                self._anon_ig_cooldown_until = time.time() + 180
            return None
        if resp.status_code != 200:
            return None
        payload = resp.json()
        user = (payload.get("data") or {}).get("user")
        if payload.get("status") == "ok" and user is None:
            raise ProfileNotFoundError(f"@{username} naam ka koi Instagram account mojood nahi hai.")
        return user

    def _get_web_tokens(self) -> Tuple[str, str, str, str]:
        now = time.time()
        if self._tokens_cache and (now - self._tokens_cache[0]) < 600:
            return (
                self._tokens_cache[1],
                self._tokens_cache[2],
                self._tokens_cache[3],
                self._tokens_cache[4],
            )

        sess = self._build_session()
        for seed_url in ("https://www.instagram.com/", "https://www.instagram.com/instagram/embed/"):
            try:
                resp = sess.get(
                    seed_url,
                    headers={
                        "Accept": "text/html",
                        "Sec-Fetch-Dest": "document",
                        "Sec-Fetch-Mode": "navigate",
                        "Sec-Fetch-Site": "none",
                    },
                    timeout=10,
                )
                csrf = sess.cookies.get("csrftoken") or self._extra_cookies.get("csrftoken", "")
                lsd_match = re.search(r'"LSD",\[\],\{[^{}]*"token":"([^"]+)"', resp.text)
                dtsg_match = re.search(r'"DTSGInitialData",\[\],\{[^{}]*"token":"([^"]+)"', resp.text)
                av_match = re.search(r'"IG_USER_EIMU":"?(\d+)', resp.text)
                lsd = lsd_match.group(1) if lsd_match else ""
                fb_dtsg = dtsg_match.group(1) if dtsg_match else ""
                av = av_match.group(1) if av_match else "0"
                if lsd:
                    self._tokens_cache = (now, csrf, lsd, fb_dtsg, av)
                    return csrf, lsd, fb_dtsg, av
            except Exception as exc:
                logger.debug("Token seed failed on %s: %s", seed_url, exc)

        return self._extra_cookies.get("csrftoken", ""), "AVqbxe3J_YA", "", "0"

    def _web_graphql(self, doc_id: str, variables: Dict[str, Any]) -> Dict[str, Any]:
        csrf, lsd, fb_dtsg, av = self._get_web_tokens()
        sess = self._build_session()
        if csrf:
            sess.cookies.set("csrftoken", csrf, domain=".instagram.com")

        headers = {
            "accept": "*/*",
            "content-type": "application/x-www-form-urlencoded",
            "origin": "https://www.instagram.com",
            "referer": "https://www.instagram.com/",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "x-csrftoken": csrf,
            "x-fb-lsd": lsd,
            "x-ig-app-id": IG_WEB_APP_ID,
        }
        data = {
            "av": av or "0",
            "lsd": lsd,
            "server_timestamps": "true",
            "doc_id": doc_id,
            "variables": json.dumps(variables, separators=(",", ":")),
        }
        if fb_dtsg:
            data["fb_dtsg"] = fb_dtsg
        resp = sess.post(
            "https://www.instagram.com/api/graphql",
            headers=headers,
            data=data,
            timeout=12,
        )
        resp.raise_for_status()
        text = resp.text
        if text.startswith("for (;;);"):
            text = text[len("for (;;);"):]
        return json.loads(text)

    def _fetch_profile_by_id_graphql(self, user_id: str) -> Optional[Dict[str, Any]]:
        variables = {
            "id": str(user_id),
            "render_surface": "PROFILE",
            "__relay_internal__pv__PolarisCannesGuardianExperienceEnabledrelayprovider": True,
            "__relay_internal__pv__PolarisCASB976ProfileEnabledrelayprovider": False,
            "__relay_internal__pv__PolarisRepostsConsumptionEnabledrelayprovider": False,
            "__relay_internal__pv__PolarisWebSchoolsEnabledrelayprovider": False,
            "enable_integrity_filters": True,
        }
        for doc_id in self.DEFAULT_DOC_IDS["profile_by_id"]:
            try:
                res = self._web_graphql(doc_id, variables)
                user = (res.get("data") or {}).get("user")
                if isinstance(user, dict) and user.get("username"):
                    return user
            except Exception as exc:
                logger.debug("profile_by_id doc_id %s failed: %s", doc_id, exc)

        # If all hardcoded doc_ids failed, auto-refresh live doc_ids once from IG bundles
        if not self._live_doc_ids_refreshed:
            self._refresh_live_doc_ids()
            for doc_id in self.DEFAULT_DOC_IDS["profile_by_id"]:
                try:
                    res = self._web_graphql(doc_id, variables)
                    user = (res.get("data") or {}).get("user")
                    if isinstance(user, dict) and user.get("username"):
                        return user
                except Exception:
                    continue
        return None

    def _search_user_id_graphql(self, username: str) -> Optional[str]:
        target_lower = username.lower()
        # 1. Authenticated web top_serp API
        try:
            sess = self._build_session()
            csrf, lsd, _, _ = self._get_web_tokens()
            resp = sess.get(
                "https://www.instagram.com/api/v1/fbsearch/web/top_serp/",
                params={"enable_metadata": "true", "query": username},
                headers={
                    "Accept": "*/*",
                    "Referer": "https://www.instagram.com/",
                    "x-ig-app-id": IG_WEB_APP_ID,
                    "x-csrftoken": csrf,
                    "x-fb-lsd": lsd,
                    "x-requested-with": "XMLHttpRequest",
                },
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                for item in (data.get("users") or []):
                    usr = item.get("user") if isinstance(item.get("user"), dict) else item
                    if isinstance(usr, dict) and usr.get("username", "").lower() == target_lower:
                        pk = usr.get("pk") or usr.get("id")
                        if pk:
                            return str(pk)
        except Exception as exc:
            logger.debug("top_serp search skipped: %s", exc)

        # 2. PolarisSearchBoxContainerQuery (logged-in search box)
        for doc_id in self.DEFAULT_DOC_IDS.get("auth_search_container", []):
            try:
                res = self._web_graphql(
                    doc_id,
                    {
                        "data": {
                            "context": "blended",
                            "include_reel": "true",
                            "query": username,
                            "search_surface": "web_top_search",
                        },
                        "hasQuery": True,
                        "query": username,
                    },
                )
                top = (res.get("data") or {}).get("xdt_api__v1__fbsearch__topsearch_connection") or {}
                for usr in top.get("users") or []:
                    u_obj = usr.get("user") if isinstance(usr.get("user"), dict) else usr
                    if isinstance(u_obj, dict) and u_obj.get("username", "").lower() == target_lower:
                        pk = u_obj.get("pk") or u_obj.get("id")
                        if pk:
                            return str(pk)
            except Exception as exc:
                logger.debug("auth_search_container %s skipped: %s", doc_id, exc)

        # 3. PolarisSearchBoxNonProfiledRefetchableQuery
        for doc_id in self.DEFAULT_DOC_IDS["search_users"]:
            try:
                res = self._web_graphql(doc_id, {"hasQuery": True, "query": username})
                serp = (
                    (res.get("data") or {}).get("xdt_api__v1__fbsearch__non_profiled_serp")
                    or {}
                )
                for usr in serp.get("users") or []:
                    if usr.get("username", "").lower() == target_lower:
                        return str(usr.get("pk") or usr.get("id"))
            except Exception as exc:
                logger.debug("search_users %s skipped: %s", doc_id, exc)
        return None

    def _get_owner_id_from_shortcode(
        self, shortcode: str, target_username: Optional[str] = None
    ) -> Optional[str]:
        variables = {
            "shortcode": shortcode,
            "__relay_internal__pv__PolarisAIGMMediaWebLabelEnabledrelayprovider": False,
        }
        for doc_id in self.DEFAULT_DOC_IDS["post_by_shortcode"]:
            try:
                res = self._web_graphql(doc_id, variables)
                web_info = (
                    (res.get("data") or {}).get("xdt_api__v1__media__shortcode__web_info")
                    or {}
                )
                items = web_info.get("items") or []
                if not items:
                    continue
                media = items[0]
                candidates: List[Dict[str, Any]] = []
                if isinstance(media.get("user"), dict):
                    candidates.append(media["user"])
                for co in media.get("coauthor_producers") or []:
                    if isinstance(co, dict):
                        candidates.append(co)
                for tag in ((media.get("usertags") or {}).get("in") or []):
                    if isinstance(tag, dict) and isinstance(tag.get("user"), dict):
                        candidates.append(tag["user"])

                if target_username is None:
                    owner = media.get("user") or {}
                    return str(owner.get("pk") or owner.get("id") or "") or None

                target_lower = target_username.lower()
                for cand in candidates:
                    if cand.get("username", "").lower() == target_lower:
                        pk = cand.get("pk") or cand.get("id")
                        if pk:
                            return str(pk)
            except Exception as exc:
                logger.debug("Shortcode lookup failed (%s): %s", shortcode, exc)
        return None

    def _resolve_user_id_via_search_bridge(self, username: str) -> Optional[str]:
        """
        Zero-login fallback for cloud/VPS datacenter IPs where direct profile HTML redirects:
        finds public post/reel shortcodes associated with `username`, verifies exact username
        ownership/tag via PolarisPostRootQuery, and returns the numeric Instagram `pk`.
        """
        shortcodes = self._find_candidate_shortcodes(username)
        for code in shortcodes[:6]:
            uid = self._get_owner_id_from_shortcode(code, target_username=username)
            if uid:
                return uid
        return None

    def _find_candidate_shortcodes(self, username: str) -> List[str]:
        found: List[str] = []
        seen = set()
        browser_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

        queries = [
            "https://search.yahoo.com/search?p="
            + urllib.parse.quote(f'site:instagram.com "{username}" ("/p/" OR "/reel/")'),
            "https://search.yahoo.com/search?p="
            + urllib.parse.quote(f"site:instagram.com/{username}/"),
            "https://search.yahoo.com/search?p="
            + urllib.parse.quote(f'site:instagram.com "{username}"'),
        ]

        direct_pat = re.compile(
            rf"instagram\.com/{re.escape(username)}/(?:p|reel|reels|tv)/([A-Za-z0-9_-]{{6,20}})",
            re.IGNORECASE,
        )
        generic_pat = re.compile(
            r"instagram\.com/(?:[A-Za-z0-9._]{1,30}/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]{6,20})",
            re.IGNORECASE,
        )

        for url in queries:
            try:
                req = urllib.request.Request(url, headers=browser_headers)
                with urllib.request.urlopen(req, timeout=8) as resp:
                    if resp.status != 200:
                        continue
                    raw_html = resp.read().decode("utf-8", errors="ignore")
                decoded = urllib.parse.unquote(raw_html)
                # Prioritize URLs explicitly containing instagram.com/<username>/(p|reel)/<code>
                for pat in (direct_pat, generic_pat):
                    for code in pat.findall(decoded):
                        if code not in seen:
                            seen.add(code)
                            found.append(code)
                if len(found) >= 4:
                    break
            except Exception as exc:
                logger.debug("Search bridge query error (%s): %s", url[:40], exc)

        return found

    def _get_storynav_session(self, force_refresh: bool = False) -> Tuple[requests.Session, str]:
        now = time.time()
        if (
            not force_refresh
            and self._storynav_session is not None
            and self._storynav_csrf
            and (now - self._storynav_ts) < 900
        ):
            return self._storynav_session, self._storynav_csrf

        sess = requests.Session()
        sess.headers.update(
            {
                "User-Agent": self._next_ua(),
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        home = sess.get("https://storynavigation.com/", timeout=10).text
        csrf_m = re.search(r'csrf-token"\s+content="([^"]+)"', home)
        tok = csrf_m.group(1) if csrf_m else ""
        if tok:
            self._storynav_session = sess
            self._storynav_csrf = tok
            self._storynav_ts = now
        return sess, tok

    def _layer3_storynav_private_capable(self, username: str) -> Optional[Dict[str, Any]]:
        """
        Zero-login Private & Public Instagram Profile Resolver (no 2-3 search rate limit!).
        Resolves `id` (pk), Followers, Following, Posts, Bio, and Private flag even for
        0-post Private accounts, then upgrades via PolarisProfilePageContentQuery for 1080p HD DP!
        """
        for attempt in range(2):
            sess, tok = self._get_storynav_session(force_refresh=(attempt > 0))
            if not tok:
                return None
            resp = sess.post(
                "https://storynavigation.com/mystorysaver-data/get-user-profile",
                json={"userName": username},
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "X-CSRF-TOKEN": tok,
                    "Origin": "https://storynavigation.com",
                    "Referer": f"https://storynavigation.com/user/{username}",
                    "Accept": "application/json",
                },
                timeout=16,
            )
            if resp.status_code == 419 and attempt == 0:
                continue
            if resp.status_code != 200:
                return None
            data = resp.json() or {}
            if not data.get("found"):
                return None
            acc = data.get("accountInfo") or {}
            if not isinstance(acc, dict):
                return None

            uid = str(acc.get("id") or "")
            posts_cnt = acc.get("mediaCount")
            is_priv = bool(data.get("isPrivate") or acc.get("isPrivate"))

            # Upgrade to official Instagram CDN 1080x1080 HD Profile Pic + verified badge!
            if uid:
                live_user = self._fetch_profile_by_id_graphql(uid)
                if live_user:
                    if live_user.get("media_count") is None and posts_cnt is not None:
                        live_user["media_count"] = posts_cnt
                    return live_user

            # Decode base64 profilePicUrl fallback if GraphQL skipped
            raw_pic = str(acc.get("profilePicUrl") or "").strip()
            pic_url = raw_pic
            if raw_pic and not raw_pic.startswith("http"):
                try:
                    pad = "=" * (-len(raw_pic) % 4)
                    decoded = base64.b64decode(raw_pic + pad).decode("utf-8", errors="ignore")
                    if decoded.startswith("http"):
                        pic_url = decoded
                except Exception:
                    pic_url = ""

            if not pic_url.startswith("http"):
                return None

            return {
                "username": str(acc.get("username") or username).lower(),
                "pk": uid,
                "full_name": str(acc.get("fullName") or ""),
                "profile_pic_url_hd": pic_url,
                "follower_count": acc.get("followedByCount"),
                "following_count": acc.get("followsCount"),
                "media_count": posts_cnt,
                "biography": str(acc.get("biography") or ""),
                "is_private": is_priv,
                "is_verified": False,
            }
        return None

    def _layer3_exact_search_fallback(self, username: str) -> Optional[Dict[str, Any]]:
        """Fallback for unindexed Private accounts via exact handle match in search index."""
        sess = requests.Session()
        sess.headers.update({
            "User-Agent": self._next_ua(),
            "Referer": "https://clipssaver.com/instagram-profile-picture-downloader",
            "Origin": "https://clipssaver.com",
        })
        tok = (
            sess.get("https://clipssaver.com/api/token", headers={"Accept": "application/json"}, timeout=7).json()
            or {}
        ).get("token")
        if not tok:
            return None
        res = sess.post(
            "https://clipssaver.com/api/instagram/instagramDownloader/search",
            json={"query": username},
            headers={"x-cs-tok": tok, "Accept": "application/json"},
            timeout=10,
        ).json()
        items = ((res or {}).get("data") or {}).get("items") or []
        target = username.lower()
        for item in items:
            if not isinstance(item, dict):
                continue
            uname = str(item.get("username") or "").strip()
            img = str(item.get("image") or "").strip()
            if uname.lower() == target and img.startswith("http") and "noavatar" not in img.lower():
                return {
                    "username": uname.lower(),
                    "pk": "",
                    "full_name": str(item.get("full_name") or ""),
                    "profile_pic_url_hd": img,
                    "follower_count": item.get("followers"),
                    "is_verified": bool(item.get("isVerified")),
                    "is_private": True,
                }
        return None

    def _layer3_residential_proxy_api(self, username: str) -> Optional[Dict[str, Any]]:
        """
        Resolve small/unindexed public Instagram profiles via ClipsSaver's residential proxy endpoint,
        then upgrade via Instagram's official PolarisProfilePageContentQuery for original 1080p CDN HD DP.
        """
        sess = requests.Session()
        sess.headers.update({
            "User-Agent": self._next_ua(),
            "Referer": "https://clipssaver.com/instagram-profile-picture-downloader",
            "Origin": "https://clipssaver.com",
        })
        tok_resp = sess.get(
            "https://clipssaver.com/api/token",
            headers={"Accept": "application/json"},
            timeout=8,
        )
        if tok_resp.status_code != 200:
            return None
        tok = (tok_resp.json() or {}).get("token")
        if not tok:
            return None

        prof_resp = sess.post(
            "https://clipssaver.com/api/instagram/instagramDownloader/profile",
            json={"username": username},
            headers={"x-cs-tok": tok, "Accept": "application/json"},
            timeout=18,
        )
        if prof_resp.status_code != 200:
            return None
        prof = ((prof_resp.json() or {}).get("data") or {}).get("profile")
        if not isinstance(prof, dict):
            return None

        uid = str(prof.get("id") or prof.get("pk") or "")
        posts_cnt = (prof.get("edge_owner_to_timeline_media") or {}).get("count")
        if posts_cnt is None:
            posts_cnt = prof.get("media_count")

        if uid:
            # Upgrade to official Instagram CDN 1080p HD URL + live counts, preserving posts_count & story flags!
            live_user = self._fetch_profile_by_id_graphql(uid)
            if live_user:
                if live_user.get("media_count") is None and posts_cnt is not None:
                    live_user["media_count"] = posts_cnt
                if not live_user.get("external_url") and prof.get("external_url"):
                    live_user["external_url"] = prof.get("external_url")
                if not live_user.get("bio_links") and prof.get("bio_links"):
                    live_user["bio_links"] = prof.get("bio_links")
                if not live_user.get("category_name") and (prof.get("category_name") or prof.get("category")):
                    live_user["category_name"] = prof.get("category_name") or prof.get("category")
                if prof.get("has_public_story") or prof.get("latest_reel_media"):
                    live_user["has_public_story"] = True
                return live_user

        # Fallback directly to residential proxy response
        return {
            "username": prof.get("username") or username.lower(),
            "pk": uid,
            "full_name": prof.get("full_name") or "",
            "profile_pic_url_hd": prof.get("profile_pic_url_hd") or prof.get("profile_pic_url"),
            "follower_count": (prof.get("edge_followed_by") or {}).get("count") or prof.get("follower_count"),
            "following_count": (prof.get("edge_follow") or {}).get("count") or prof.get("following_count"),
            "media_count": posts_cnt,
            "biography": prof.get("biography") or "",
            "external_url": prof.get("external_url") or "",
            "bio_links": prof.get("bio_links") or [],
            "category_name": prof.get("category_name") or prof.get("category") or "",
            "has_public_story": bool(prof.get("has_public_story") or prof.get("latest_reel_media")),
            "is_private": prof.get("is_private", False),
            "is_verified": prof.get("is_verified", False),
        }

    def search_similar_usernames(self, query: str) -> List[str]:
        """Return up to 4 real matching Instagram usernames when an exact handle fails."""
        try:
            sess = requests.Session()
            sess.headers.update({
                "User-Agent": DEFAULT_UA,
                "Referer": "https://clipssaver.com/instagram-profile-picture-downloader",
                "Origin": "https://clipssaver.com",
            })
            tok = (
                sess.get(
                    "https://clipssaver.com/api/token",
                    headers={"Accept": "application/json"},
                    timeout=6,
                ).json()
                or {}
            ).get("token")
            if not tok:
                return []
            clean_q = query.replace(".", "").strip("_") or query
            res = sess.post(
                "https://clipssaver.com/api/instagram/instagramDownloader/search",
                json={"query": query},
                headers={"x-cs-tok": tok, "Accept": "application/json"},
                timeout=10,
            ).json()
            items = ((res or {}).get("data") or {}).get("items") or []
            if not items and clean_q != query:
                tok2 = (
                    sess.get(
                        "https://clipssaver.com/api/token",
                        headers={"Accept": "application/json"},
                        timeout=6,
                    ).json()
                    or {}
                ).get("token")
                if tok2:
                    res2 = sess.post(
                        "https://clipssaver.com/api/instagram/instagramDownloader/search",
                        json={"query": clean_q},
                        headers={"x-cs-tok": tok2, "Accept": "application/json"},
                        timeout=10,
                    ).json()
                    items = ((res2 or {}).get("data") or {}).get("items") or []
            out: List[str] = []
            for item in items:
                uname = (item.get("username") or "").strip()
                if uname and uname.lower() != query.lower() and uname not in out:
                    out.append(uname)
                if len(out) >= 4:
                    break
            return out
        except Exception as exc:
            logger.debug("search_similar_usernames skipped: %s", exc)
            return []

    def _layer3c_public_mirror(self, username: str) -> Optional[Dict[str, Any]]:
        """Fallback to public HD profile pic mirror when post bridge has no indexed shortcodes."""
        resp = requests.get(
            f"https://greatfon.io/v/{username}",
            headers={"User-Agent": DEFAULT_UA},
            timeout=8,
        )
        if resp.status_code != 200:
            return None
        pic_match = re.search(r"https://media\.dumpor\.io/[^\"'\s>]+", resp.text)
        if not pic_match:
            return None
        return {
            "username": username.lower(),
            "pk": "",
            "profile_pic_url_hd": pic_match.group(0),
        }

    def _refresh_live_doc_ids(self) -> None:
        """Auto-discover updated Relay operation doc_ids from Instagram's live JS bundles."""
        self._live_doc_ids_refreshed = True
        try:
            sess = self._build_session()
            raw = sess.get("https://www.instagram.com/instagram/embed/", timeout=10).text
            js_urls = list(
                dict.fromkeys(
                    re.findall(
                        r"https://static\.cdninstagram\.com/rsrc\.php/[^\"'\s>]+?\.js",
                        raw.replace("\\/", "/"),
                    )
                )
            )
            op_map = {
                "PolarisProfilePageContentQuery_instagramRelayOperation": "profile_by_id",
                "PolarisPostRootQuery_instagramRelayOperation": "post_by_shortcode",
                "PolarisSearchBoxNonProfiledRefetchableQuery_instagramRelayOperation": "search_users",
            }
            for url in js_urls[:80]:
                try:
                    js = sess.get(url, timeout=6).text
                except Exception:
                    continue
                for op_name, bucket in op_map.items():
                    m = re.search(
                        rf'__d\("{op_name}",\[\],\(function\([^)]*\)\{{[a-z]\.exports="(\d+)"\}}',
                        js,
                    )
                    if m:
                        new_id = m.group(1)
                        if new_id not in self.DEFAULT_DOC_IDS[bucket]:
                            self.DEFAULT_DOC_IDS[bucket].insert(0, new_id)
        except Exception as exc:
            logger.debug("Live doc_id refresh skipped: %s", exc)

    def _layer4_instaloader(self, username: str) -> Optional[Dict[str, Any]]:
        import instaloader

        loader = instaloader.Instaloader(
            download_pictures=False,
            download_videos=False,
            download_video_thumbnails=False,
            save_metadata=False,
            quiet=True,
        )
        if self.session_id:
            loader.context._session.cookies.set(
                "sessionid", self.session_id, domain=".instagram.com"
            )
        try:
            profile = instaloader.Profile.from_username(loader.context, username)
        except instaloader.ProfileNotExistsException as exc:
            raise ProfileNotFoundError(
                f"@{username} naam ka koi Instagram account mojood nahi hai."
            ) from exc

        return {
            "username": profile.username,
            "pk": str(profile.userid),
            "full_name": profile.full_name,
            "profile_pic_url_hd": profile.profile_pic_url,
            "follower_count": profile.followers,
            "following_count": profile.followees,
            "media_count": profile.mediacount,
            "biography": profile.biography,
            "is_private": profile.is_private,
            "is_verified": profile.is_verified,
        }

    def _download_image_bytes(self, url: str) -> bytes:
        data = self.download_media_bytes(url, max_bytes=20 * 1024 * 1024)
        if not data or len(data) < 500:
            raise InstagramFetchError("Downloaded image file empty ya corrupt hai.")
        return data

    # ------------------------------------------------------------------
    # Full Media Suite: Stories, Posts, Reels, Carousels & Highlights
    # ------------------------------------------------------------------

    @staticmethod
    def _shortcode_to_media_id(shortcode: str) -> Optional[str]:
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        clean = (shortcode or "").strip()
        # Private post shortcodes can exceed 11 chars; first 11 chars encode the 64-bit media_id
        if not clean:
            return None
        code = clean[:11]
        media_id = 0
        for ch in code:
            idx = alphabet.find(ch)
            if idx < 0:
                return None
            media_id = (media_id * 64) + idx
        return str(media_id) if media_id > 0 else None

    @staticmethod
    def _decode_proxy_src_url(url: str) -> Optional[str]:
        """Extract direct Instagram CDN URL from `cdn.socialhubapi.com/...?src=<b64>` if present."""
        try:
            parsed = urllib.parse.urlparse(url)
            qs = urllib.parse.parse_qs(parsed.query)
            b64_src = (qs.get("src") or [None])[0]
            if not b64_src:
                return None
            pad = "=" * (-len(b64_src) % 4)
            decoded = base64.urlsafe_b64decode(b64_src + pad).decode("utf-8", errors="ignore")
            return decoded if decoded.startswith("http") else None
        except Exception:
            return None

    def download_media_bytes(self, url: str, max_bytes: int = 48 * 1024 * 1024) -> Optional[bytes]:
        """Download photo/video bytes safely within Telegram's 50 MB Bot API limit."""
        candidates = [url]
        fallback_cdn = self._decode_proxy_src_url(url)
        if fallback_cdn and fallback_cdn not in candidates:
            candidates.append(fallback_cdn)

        for target in candidates:
            try:
                with requests.get(
                    target,
                    headers={
                        "User-Agent": DEFAULT_UA,
                        "Referer": "https://www.instagram.com/",
                        "Accept": "*/*",
                    },
                    stream=True,
                    timeout=25,
                ) as resp:
                    if resp.status_code != 200:
                        continue
                    cl = resp.headers.get("Content-Length")
                    if cl and cl.isdigit() and int(cl) > max_bytes:
                        return None
                    chunks: List[bytes] = []
                    total = 0
                    for chunk in resp.iter_content(chunk_size=65536):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > max_bytes:
                            return None
                        chunks.append(chunk)
                    data = b"".join(chunks)
                    if len(data) >= 400:
                        return data
            except Exception as exc:
                logger.debug("download_media_bytes candidate failed (%s): %s", target[:55], exc)
        return None

    def _clipssaver_request(
        self, endpoint: str, payload: Dict[str, Any], timeout: int = 35
    ) -> Dict[str, Any]:
        last_exc: Optional[Exception] = None
        for attempt in range(2):
            try:
                sess = requests.Session()
                sess.headers.update(
                    {
                        "User-Agent": self._next_ua(),
                        "Referer": "https://clipssaver.com/instagram-video-downloader",
                        "Origin": "https://clipssaver.com",
                    }
                )
                tok_resp = sess.get(
                    "https://clipssaver.com/api/token",
                    headers={"Accept": "application/json"},
                    timeout=10,
                )
                tok_resp.raise_for_status()
                tok = (tok_resp.json() or {}).get("token")
                if not tok:
                    raise InstagramFetchError("Media gateway token nahi mil paya.")

                resp = sess.post(
                    f"https://clipssaver.com/api/instagram/instagramDownloader/{endpoint}",
                    json=payload,
                    headers={"x-cs-tok": tok, "Accept": "application/json"},
                    timeout=timeout,
                )
                if resp.status_code == 200:
                    data = resp.json() or {}
                    if data:
                        return data
                if attempt == 0:
                    time.sleep(0.8)
            except Exception as exc:
                last_exc = exc
                if attempt == 0:
                    time.sleep(0.8)
        if last_exc:
            logger.debug("clipssaver %s failed after retry: %s", endpoint, last_exc)
        return {}

    def _resolve_user_id_fast(self, username: str) -> Optional[str]:
        clean = username.strip().lstrip("@").lower()
        cached = self.get_cached_by_username(clean)
        if cached and cached.user_id:
            return cached.user_id
        try:
            res = self.get_profile_pic(clean)
            if res.user_id:
                return res.user_id
        except Exception:
            pass
        return None

    @staticmethod
    def _parse_v1_media_node(item: Dict[str, Any]) -> List[MediaItem]:
        """Parse Instagram `/api/v1/` item (single or carousel_media) into MediaItem list."""
        carousel = item.get("carousel_media")
        if isinstance(carousel, list) and carousel:
            out: List[MediaItem] = []
            for sub in carousel:
                if isinstance(sub, dict):
                    out.extend(InstagramService._parse_v1_media_node(sub))
            return out

        media_type = item.get("media_type")
        video_versions = item.get("video_versions") or []
        img_candidates = ((item.get("image_versions2") or {}).get("candidates")) or []
        best_img = ""
        if img_candidates:
            best = max(img_candidates, key=lambda c: c.get("width", 0) if isinstance(c, dict) else 0)
            best_img = best.get("url") or ""

        is_vid = bool(media_type == 2 or video_versions)
        if is_vid and video_versions:
            best_vid = max(video_versions, key=lambda v: v.get("width", 0) if isinstance(v, dict) else 0)
            v_url = best_vid.get("url") or ""
            if v_url:
                return [
                    MediaItem(
                        is_video=True,
                        media_url=v_url,
                        thumbnail_url=best_img,
                        shortcode=str(item.get("code") or ""),
                        taken_at=item.get("taken_at"),
                    )
                ]
        if best_img:
            return [
                MediaItem(
                    is_video=False,
                    media_url=best_img,
                    thumbnail_url=best_img,
                    shortcode=str(item.get("code") or ""),
                    taken_at=item.get("taken_at"),
                )
            ]
        return []

    @staticmethod
    def _parse_graphql_or_proxy_post(node: Dict[str, Any], fallback_shortcode: str = "") -> PostDownloadResult:
        shortcode = str(node.get("shortcode") or node.get("short_code") or node.get("shortCode") or fallback_shortcode)
        owner = node.get("owner") if isinstance(node.get("owner"), dict) else {}
        owner_username = str(owner.get("username") or "").strip()
        owner_full_name = str(owner.get("full_name") or owner.get("fullName") or "").strip()

        # Caption extraction
        caption = ""
        cap_edges = ((node.get("edge_media_to_caption") or {}).get("edges")) or []
        if cap_edges and isinstance(cap_edges[0], dict):
            caption = str(((cap_edges[0].get("node") or {}).get("text")) or "").strip()
        if not caption and isinstance(node.get("description"), str):
            caption = node["description"].strip()

        likes = (
            ((node.get("edge_media_preview_like") or {}).get("count"))
            if isinstance(node.get("edge_media_preview_like"), dict)
            else None
        )
        if likes is None and isinstance(node.get("edge_liked_by"), dict):
            likes = node["edge_liked_by"].get("count")
        if likes is None:
            likes = node.get("like_count") if node.get("like_count") is not None else node.get("likeCount")

        comments = (
            ((node.get("edge_media_to_comment") or {}).get("count"))
            if isinstance(node.get("edge_media_to_comment"), dict)
            else None
        )
        if comments is None:
            comments = node.get("comment_count") if node.get("comment_count") is not None else node.get("commentCount")

        views = node.get("video_view_count") or node.get("play_count") or node.get("playCount") or node.get("videoViewCount")
        taken_at = node.get("taken_at_timestamp") or node.get("created_at") or node.get("createdAt")

        items: List[MediaItem] = []
        sidecar_edges = ((node.get("edge_sidecar_to_children") or {}).get("edges")) or []
        if sidecar_edges:
            for edge in sidecar_edges:
                child = edge.get("node") if isinstance(edge, dict) and isinstance(edge.get("node"), dict) else edge
                if not isinstance(child, dict):
                    continue
                c_vid = str(
                    child.get("video_url")
                    or child.get("videoUrl")
                    or (child.get("download_url") if child.get("is_video") else "")
                    or ""
                )
                c_img = str(
                    child.get("display_url")
                    or child.get("imageUrl")
                    or child.get("url")
                    or child.get("thumbnail_src")
                    or ""
                )
                is_v = bool(child.get("is_video") or child.get("__typename") == "GraphVideo" or c_vid)
                m_url = c_vid if (is_v and c_vid) else c_img
                if m_url:
                    items.append(
                        MediaItem(
                            is_video=bool(is_v and c_vid),
                            media_url=m_url,
                            thumbnail_url=c_img,
                            shortcode=str(child.get("shortcode") or shortcode),
                            taken_at=taken_at if isinstance(taken_at, int) else None,
                        )
                    )
        else:
            vid_url = str(node.get("video_url") or node.get("videoUrl") or "")
            img_url = str(
                node.get("display_url")
                or node.get("imageUrl")
                or node.get("thumbnail_src")
                or node.get("url")
                or ""
            )
            dl_url = str(node.get("download_url") or node.get("downloadUrl") or "")
            is_v = bool(node.get("is_video") or node.get("__typename") == "GraphVideo" or vid_url)
            m_url = (vid_url or dl_url) if is_v else (img_url or dl_url)
            if m_url:
                items.append(
                    MediaItem(
                        is_video=is_v,
                        media_url=m_url,
                        thumbnail_url=img_url,
                        shortcode=shortcode,
                        taken_at=taken_at if isinstance(taken_at, int) else None,
                    )
                )

        return PostDownloadResult(
            shortcode=shortcode,
            owner_username=owner_username,
            owner_full_name=owner_full_name,
            caption=caption,
            like_count=int(likes) if isinstance(likes, (int, float)) and likes >= 0 else None,
            comment_count=int(comments) if isinstance(comments, (int, float)) and comments >= 0 else None,
            view_count=int(views) if isinstance(views, (int, float)) and views >= 0 else None,
            taken_at=int(taken_at) if isinstance(taken_at, (int, float)) and taken_at > 0 else None,
            items=items,
        )

    def download_post_or_reel(self, url_or_shortcode: str) -> PostDownloadResult:
        """Download any Instagram Post, Reel, Video, or Multi-slide Carousel."""
        raw = (url_or_shortcode or "").strip()
        shortcode = raw
        if "instagram.com" in raw.lower():
            kind, val1, _ = classify_instagram_input(raw)
            if kind != "post":
                raise InvalidUsernameError("Ye valid Instagram Post ya Reel link nahi hai.")
            shortcode = val1
        else:
            shortcode = shortcode.strip("/")

        # Layer 1: Authenticated Instagram v1 media info (supports private followed accounts!)
        if self.session_id:
            media_id = self._shortcode_to_media_id(shortcode)
            if media_id:
                try:
                    sess = self._build_session()
                    csrf, lsd, _, _ = self._get_web_tokens()
                    resp = sess.get(
                        f"https://www.instagram.com/api/v1/media/{media_id}/info/",
                        headers={
                            "Accept": "*/*",
                            "Referer": f"https://www.instagram.com/p/{shortcode}/",
                            "x-ig-app-id": IG_WEB_APP_ID,
                            "x-csrftoken": csrf,
                            "x-fb-lsd": lsd,
                            "x-requested-with": "XMLHttpRequest",
                        },
                        timeout=12,
                    )
                    if resp.status_code == 200:
                        v1_items = (resp.json() or {}).get("items") or []
                        if v1_items and isinstance(v1_items[0], dict):
                            it = v1_items[0]
                            parsed_media = self._parse_v1_media_node(it)
                            if parsed_media:
                                usr = it.get("user") or {}
                                cap_obj = it.get("caption") or {}
                                return PostDownloadResult(
                                    shortcode=str(it.get("code") or shortcode),
                                    owner_username=str(usr.get("username") or ""),
                                    owner_full_name=str(usr.get("full_name") or ""),
                                    caption=str(cap_obj.get("text") or "").strip() if isinstance(cap_obj, dict) else "",
                                    like_count=it.get("like_count"),
                                    comment_count=it.get("comment_count"),
                                    view_count=it.get("play_count") or it.get("view_count"),
                                    taken_at=it.get("taken_at"),
                                    items=parsed_media,
                                )
                except Exception as exc:
                    logger.debug("Auth v1 media info skipped for %s: %s", shortcode, exc)

        # Layer 2: Direct GraphQL PolarisPostRootQuery
        for doc_id in self.DEFAULT_DOC_IDS["post_by_shortcode"]:
            try:
                res = self._web_graphql(
                    doc_id,
                    {
                        "shortcode": shortcode,
                        "__relay_internal__pv__PolarisAIGMMediaWebLabelEnabledrelayprovider": False,
                    },
                )
                gql_media = (res.get("data") or {}).get("xdt_shortcode_media")
                if isinstance(gql_media, dict) and (gql_media.get("display_url") or gql_media.get("video_url")):
                    parsed = self._parse_graphql_or_proxy_post(gql_media, shortcode)
                    if parsed.items:
                        return parsed
            except Exception as exc:
                logger.debug("GraphQL post lookup skipped (%s): %s", shortcode, exc)

        # Layer 3: Zero-login Residential Proxy Post/Reel Downloader
        for path_kind in ("p", "reel"):
            try:
                res = self._clipssaver_request(
                    "download-post",
                    {"url": f"https://www.instagram.com/{path_kind}/{shortcode}/"},
                )
                post_node = ((res.get("data") or {}).get("post")) if isinstance(res.get("data"), dict) else None
                if isinstance(post_node, dict):
                    parsed = self._parse_graphql_or_proxy_post(post_node, shortcode)
                    if parsed.items:
                        return parsed
            except Exception as exc:
                logger.debug("Proxy download-post (%s) failed for %s: %s", path_kind, shortcode, exc)

        if not self.session_id:
            raise InstagramFetchError(
                "Ye Post/Reel load nahi ho payi! Agar ye Private account ki post hai toh /login se connect karein."
            )
        raise InstagramFetchError("Ye Post/Reel nahi mili ya Private account hai jise aap follow nahi karte.")

    def get_user_stories(self, username: str, story_id: Optional[str] = None) -> List[MediaItem]:
        """Fetch active 24h Instagram Stories (Photos & Videos) for `username`."""
        clean = username.strip().lstrip("@").lower()
        items: List[MediaItem] = []

        # Layer 1: Authenticated Instagram Stories API (supports private followed accounts!)
        if self.session_id:
            uid = self._resolve_user_id_fast(clean)
            if uid:
                try:
                    sess = self._build_session()
                    csrf, lsd, _, _ = self._get_web_tokens()
                    resp = sess.get(
                        "https://www.instagram.com/api/v1/feed/reels_media/",
                        params={"reel_ids": uid},
                        headers={
                            "Accept": "*/*",
                            "Referer": f"https://www.instagram.com/stories/{clean}/",
                            "x-ig-app-id": IG_WEB_APP_ID,
                            "x-csrftoken": csrf,
                            "x-fb-lsd": lsd,
                            "x-requested-with": "XMLHttpRequest",
                        },
                        timeout=12,
                    )
                    if resp.status_code == 200:
                        reels = (resp.json() or {}).get("reels") or {}
                        reel_obj = reels.get(str(uid)) or {}
                        raw_items = reel_obj.get("items") or []
                        for st_item in raw_items:
                            if isinstance(st_item, dict):
                                parsed = self._parse_v1_media_node(st_item)
                                for m in parsed:
                                    m.shortcode = str(st_item.get("pk") or st_item.get("id") or "").split("_")[0]
                                    items.append(m)
                except Exception as exc:
                    logger.debug("Auth stories lookup skipped for %s: %s", clean, exc)

        # Layer 2: Zero-login Residential Proxy Stories API
        if not items:
            try:
                res = self._clipssaver_request("stories", {"username": clean})
                st_list = ((res.get("data") or {}).get("stories")) if isinstance(res.get("data"), dict) else None
                if isinstance(st_list, list):
                    for st in st_list:
                        if not isinstance(st, dict):
                            continue
                        is_vid = bool(st.get("isVideo") or st.get("videoUrl"))
                        m_url = str(
                            (st.get("videoUrl") or st.get("downloadUrl"))
                            if is_vid
                            else (st.get("displayUrl") or st.get("downloadUrl") or st.get("url"))
                            or ""
                        )
                        if m_url:
                            sid = str(st.get("id") or st.get("post_id") or "").split("_")[0]
                            items.append(
                                MediaItem(
                                    is_video=is_vid,
                                    media_url=m_url,
                                    thumbnail_url=str(st.get("displayUrl") or ""),
                                    shortcode=sid,
                                    taken_at=st.get("takenAt") or st.get("createdAt"),
                                )
                            )
            except Exception as exc:
                logger.debug("Proxy stories lookup skipped for %s: %s", clean, exc)

        if story_id and items:
            matched = [it for it in items if story_id in (it.shortcode or "")]
            if matched:
                return matched
        return items

    def get_recent_posts(self, username: str, limit: int = 8) -> List[PostSummary]:
        """Fetch recent timeline posts (Photos, Videos, Carousels) for `username`."""
        clean = username.strip().lstrip("@").lower()
        summaries: List[PostSummary] = []

        # Layer 1: Authenticated user feed (supports private followed accounts!)
        if self.session_id:
            uid = self._resolve_user_id_fast(clean)
            if uid:
                try:
                    sess = self._build_session()
                    csrf, lsd, _, _ = self._get_web_tokens()
                    resp = sess.get(
                        f"https://www.instagram.com/api/v1/feed/user/{uid}/",
                        params={"count": str(limit)},
                        headers={
                            "Accept": "*/*",
                            "Referer": f"https://www.instagram.com/{clean}/",
                            "x-ig-app-id": IG_WEB_APP_ID,
                            "x-csrftoken": csrf,
                            "x-fb-lsd": lsd,
                            "x-requested-with": "XMLHttpRequest",
                        },
                        timeout=12,
                    )
                    if resp.status_code == 200:
                        for it in ((resp.json() or {}).get("items") or [])[:limit]:
                            if not isinstance(it, dict) or not it.get("code"):
                                continue
                            cap_obj = it.get("caption") or {}
                            cap_txt = str(cap_obj.get("text") or "").strip() if isinstance(cap_obj, dict) else ""
                            summaries.append(
                                PostSummary(
                                    shortcode=str(it["code"]),
                                    is_video=bool(it.get("media_type") == 2),
                                    is_carousel=bool(it.get("media_type") == 8 or it.get("carousel_media")),
                                    caption=cap_txt,
                                    like_count=it.get("like_count"),
                                    view_count=it.get("play_count") or it.get("view_count"),
                                    taken_at=it.get("taken_at"),
                                )
                            )
                        if summaries:
                            return summaries
                except Exception as exc:
                    logger.debug("Auth feed lookup skipped for %s: %s", clean, exc)

        # Layer 2: Zero-login Residential Proxy Posts API
        try:
            res = self._clipssaver_request("posts", {"username": clean, "limit": 10})
            edges = (
                (((((res.get("data") or {}).get("posts") or {}).get("data") or {}).get("user") or {})
                 .get("edge_owner_to_timeline_media") or {})
                .get("edges")
            ) or []
            for edge in edges[:limit]:
                node = edge.get("node") if isinstance(edge, dict) else None
                if not isinstance(node, dict) or not node.get("shortcode"):
                    continue
                cap_edges = ((node.get("edge_media_to_caption") or {}).get("edges")) or []
                cap_txt = (
                    str(((cap_edges[0].get("node") or {}).get("text")) or "").strip()
                    if cap_edges and isinstance(cap_edges[0], dict)
                    else ""
                )
                likes = ((node.get("edge_media_preview_like") or {}).get("count"))
                tname = str(node.get("__typename") or "")
                summaries.append(
                    PostSummary(
                        shortcode=str(node["shortcode"]),
                        is_video=bool(node.get("is_video") or tname == "GraphVideo"),
                        is_carousel=bool(tname == "GraphSidecar" or node.get("edge_sidecar_to_children")),
                        caption=cap_txt,
                        like_count=likes if isinstance(likes, int) else None,
                        view_count=node.get("play_count"),
                        taken_at=node.get("taken_at_timestamp"),
                    )
                )
        except Exception as exc:
            logger.debug("Proxy posts lookup skipped for %s: %s", clean, exc)

        return summaries

    def get_recent_reels(self, username: str, limit: int = 8) -> List[PostSummary]:
        """Fetch recent Reels for `username`."""
        clean = username.strip().lstrip("@").lower()
        out: List[PostSummary] = []
        try:
            res = self._clipssaver_request("reels", {"username": clean, "limit": 10})
            reels_list = ((res.get("data") or {}).get("reels")) if isinstance(res.get("data"), dict) else None
            if isinstance(reels_list, list):
                for rl in reels_list[:limit]:
                    if not isinstance(rl, dict):
                        continue
                    sc = str(rl.get("shortCode") or rl.get("short_code") or "").strip()
                    if not sc:
                        continue
                    out.append(
                        PostSummary(
                            shortcode=sc,
                            is_video=True,
                            is_carousel=False,
                            caption=str(rl.get("description") or "").strip(),
                            like_count=rl.get("likeCount") if rl.get("likeCount") is not None else rl.get("like_count"),
                            view_count=rl.get("playCount") or rl.get("videoViewCount"),
                            taken_at=rl.get("createdAt") or rl.get("created_at"),
                        )
                    )
        except Exception as exc:
            logger.debug("Proxy reels lookup skipped for %s: %s", clean, exc)

        if not out:
            # Fallback to video posts from recent posts
            for p in self.get_recent_posts(clean, limit=12):
                if p.is_video:
                    out.append(p)
                if len(out) >= limit:
                    break
        return out

    def get_user_highlights(self, username: str) -> List[HighlightSummary]:
        """Fetch Story Highlights list for `username`."""
        clean = username.strip().lstrip("@").lower()
        out: List[HighlightSummary] = []

        if self.session_id:
            uid = self._resolve_user_id_fast(clean)
            if uid:
                try:
                    sess = self._build_session()
                    csrf, lsd, _, _ = self._get_web_tokens()
                    resp = sess.get(
                        f"https://www.instagram.com/api/v1/highlights/{uid}/highlights_tray/",
                        headers={
                            "Accept": "*/*",
                            "Referer": f"https://www.instagram.com/{clean}/",
                            "x-ig-app-id": IG_WEB_APP_ID,
                            "x-csrftoken": csrf,
                            "x-fb-lsd": lsd,
                            "x-requested-with": "XMLHttpRequest",
                        },
                        timeout=12,
                    )
                    if resp.status_code == 200:
                        for tr in ((resp.json() or {}).get("tray") or [])[:12]:
                            if not isinstance(tr, dict) or not tr.get("id"):
                                continue
                            hid = str(tr["id"]).replace("highlight:", "")
                            out.append(HighlightSummary(highlight_id=hid, title=str(tr.get("title") or "Highlight")))
                        if out:
                            return out
                except Exception as exc:
                    logger.debug("Auth highlights tray skipped for %s: %s", clean, exc)

        try:
            res = self._clipssaver_request("highlights", {"username": clean})
            hlist = ((res.get("data") or {}).get("highlights")) if isinstance(res.get("data"), dict) else None
            if isinstance(hlist, list):
                for hl in hlist[:12]:
                    if isinstance(hl, dict) and hl.get("id"):
                        hid = str(hl["id"]).replace("highlight:", "")
                        out.append(HighlightSummary(highlight_id=hid, title=str(hl.get("title") or "Highlight")))
        except Exception as exc:
            logger.debug("Proxy highlights lookup skipped for %s: %s", clean, exc)

        return out

    def get_highlight_items(self, username: str, highlight_id: str) -> List[MediaItem]:
        """Fetch media items inside a specific Story Highlight."""
        clean = username.strip().lstrip("@").lower()
        hid = str(highlight_id).replace("highlight:", "").strip()
        items: List[MediaItem] = []

        if self.session_id and hid:
            try:
                sess = self._build_session()
                csrf, lsd, _, _ = self._get_web_tokens()
                resp = sess.get(
                    "https://www.instagram.com/api/v1/feed/reels_media/",
                    params={"reel_ids": f"highlight:{hid}"},
                    headers={
                        "Accept": "*/*",
                        "Referer": f"https://www.instagram.com/{clean}/",
                        "x-ig-app-id": IG_WEB_APP_ID,
                        "x-csrftoken": csrf,
                        "x-fb-lsd": lsd,
                        "x-requested-with": "XMLHttpRequest",
                    },
                    timeout=12,
                )
                if resp.status_code == 200:
                    reels = (resp.json() or {}).get("reels") or {}
                    reel_obj = reels.get(f"highlight:{hid}") or {}
                    for st_item in (reel_obj.get("items") or []):
                        if isinstance(st_item, dict):
                            items.extend(self._parse_v1_media_node(st_item))
                    if items:
                        return items
            except Exception as exc:
                logger.debug("Auth highlight-media skipped (%s): %s", hid, exc)

        try:
            res = self._clipssaver_request("highlight-media", {"username": clean, "id": hid})
            raw_list = res.get("data") if isinstance(res.get("data"), list) else []
            for st in raw_list:
                if not isinstance(st, dict):
                    continue
                is_vid = bool(st.get("isVideo") or st.get("videoUrl"))
                m_url = str(
                    (st.get("videoUrl") or st.get("downloadUrl"))
                    if is_vid
                    else (st.get("displayUrl") or st.get("downloadUrl") or st.get("url"))
                    or ""
                )
                if m_url:
                    items.append(
                        MediaItem(
                            is_video=is_vid,
                            media_url=m_url,
                            thumbnail_url=str(st.get("displayUrl") or ""),
                            taken_at=st.get("takenAt") or st.get("createdAt"),
                        )
                    )
        except Exception as exc:
            logger.debug("Proxy highlight-media skipped (%s): %s", hid, exc)

        return items

