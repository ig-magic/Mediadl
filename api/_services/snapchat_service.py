"""
Snapchat Public Profile, Story, Highlight & Spotlight Downloader Service
========================================================================
Zero-login fetcher using Snapchat's public web SSR (`__NEXT_DATA__` & OpenGraph)
on `https://www.snapchat.com/@<username>` and `/spotlight/<id>`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import html as htmllib
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple
import requests

from .instagram_service import InstagramFetchError, MediaItem

logger = logging.getLogger(__name__)

SNAP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)
SNAP_USER_RE = re.compile(r"^[A-Za-z0-9._-]{3,35}$")


class SnapchatError(InstagramFetchError):
    """User-friendly Snapchat fetch error."""


@dataclass
class SnapHighlight:
    index: int
    highlight_id: str
    title: str
    snaps: List[MediaItem] = field(default_factory=list)


@dataclass
class SnapProfileResult:
    username: str
    display_name: str
    bio: str
    subscribers: Optional[int]
    is_verified: bool
    website_url: str
    address: str
    is_public_creator: bool
    avatar_url: str
    avatar_bytes: bytes
    background_url: str = ""
    background_bytes: Optional[bytes] = None
    snapcode_url: str = ""
    has_bitmoji_3d: bool = False
    stories: List[MediaItem] = field(default_factory=list)
    highlights: List[SnapHighlight] = field(default_factory=list)
    spotlights: List[MediaItem] = field(default_factory=list)


def parse_snapchat_input(raw_text: str) -> Tuple[str, str]:
    """
    Parse user input into:
      - ('spotlight_url', full_url) for /spotlight/... or t.snapchat.com/... share links
      - ('username', username)      for @username, username, snapchat.com/@user, snapchat.com/add/user
    """
    text = (raw_text or "").strip()
    if text.startswith("/"):
        parts = text.split(maxsplit=1)
        text = parts[1].strip() if len(parts) > 1 else ""
    if not text:
        raise SnapchatError("Kripya Snapchat username ya link bhejein! Example: `/snap khloekardashian`")

    # Short share link t.snapchat.com/... or snapchat.com/spotlight/... or snapchat.com/@user/spotlight/...
    m_share = re.search(
        r"((?:https?://)?(?:t\.snapchat\.com/[A-Za-z0-9_-]+|(?:www\.)?snapchat\.com/(?:@[A-Za-z0-9._-]+/spotlight|(?:t|spotlight|p))/[^\s]+))",
        text,
        re.I,
    )
    if m_share:
        url = m_share.group(1)
        if not url.startswith("http"):
            url = f"https://{url}"
        return ("spotlight_url", url)

    m_url = re.search(
        r"(?:https?://)?(?:www\.|story\.)?snapchat\.com/(?:add/|@|s/)?([A-Za-z0-9._-]+)",
        text,
        re.I,
    )
    if m_url:
        cand = m_url.group(1).lstrip("@").strip()
        if SNAP_USER_RE.match(cand) and cand.lower() not in {"add", "spotlight", "discover", "p", "t"}:
            return ("username", cand.lower())

    cand = text.lstrip("@").strip().strip("/")
    if SNAP_USER_RE.match(cand):
        return ("username", cand.lower())

    raise SnapchatError("Invalid Snapchat username ya link! Example: `/snap mrbeast`")


def _upgrade_snap_avatar_to_1080p(url: str) -> str:
    if not url:
        return ""
    # If it's a cropped 3D Bitmoji WebP (`?scale=0&trim=circle&ua=2`), upgrade to full uncropped PNG!
    if "/3d/render/" in url and ".webp" in url:
        import base64
        full_webp = re.sub(r"[?&]scale=0", "", url)
        full_webp = re.sub(r"[?&]trim=circle", "", full_webp)
        if "?" not in full_webp and "&" in full_webp:
            full_webp = full_webp.replace("&", "?", 1)
        b64 = base64.urlsafe_b64encode(full_webp.encode("utf-8")).decode("ascii").rstrip("=")
        return f"https://cf-st.sc-cdn.net/aps/bolt/{b64}._FMpng"
    # Upgrade `._RS0,90_FMjpeg` or `._RS0,300_FMpng` -> `._RS0,1080_FMjpeg`
    return re.sub(r"\._RS0,\d+_", "._RS0,1080_", url)


def _parse_snap_list(snap_list: Any) -> List[MediaItem]:
    if not isinstance(snap_list, list):
        return []
    out: List[MediaItem] = []
    for sn in snap_list:
        if not isinstance(sn, dict):
            continue
        urls = sn.get("snapUrls") or {}
        m_url = str(urls.get("mediaUrl") or "").strip()
        if not m_url:
            continue
        prev_obj = urls.get("mediaPreviewUrl")
        prev_url = str(prev_obj.get("value") if isinstance(prev_obj, dict) else (prev_obj or "")).strip()
        media_type = sn.get("snapMediaType")
        # On Snapchat web: snapMediaType 1 = Video, 0 = Image
        is_vid = bool(media_type == 1)
        ts_obj = sn.get("timestampInSec")
        ts_val = ts_obj.get("value") if isinstance(ts_obj, dict) else ts_obj
        ts_int = int(ts_val) if str(ts_val or "").isdigit() else None
        sid_obj = sn.get("snapId")
        sid = str(sid_obj.get("value") if isinstance(sid_obj, dict) else (sid_obj or ""))
        out.append(
            MediaItem(
                is_video=is_vid,
                media_url=m_url,
                thumbnail_url=prev_url,
                shortcode=sid[:24],
                taken_at=ts_int,
            )
        )
    return out


class SnapchatService:
    """Fetches Snapchat Public Profiles, 1080p DPs, Active Stories, Highlights & Spotlights."""

    def __init__(self, cache_ttl: int = 300) -> None:
        self.cache_ttl = cache_ttl
        self._cache: Dict[str, Tuple[float, SnapProfileResult]] = {}

    def _new_session(self) -> requests.Session:
        sess = requests.Session()
        sess.headers.update(
            {
                "User-Agent": SNAP_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        return sess

    def get_profile(self, username: str) -> SnapProfileResult:
        clean = username.strip().lstrip("@").lower()
        now = time.time()
        cached = self._cache.get(clean)
        if cached and (now - cached[0]) < self.cache_ttl:
            return cached[1]

        sess = self._new_session()
        resp = sess.get(f"https://www.snapchat.com/@{clean}", timeout=15)
        if resp.status_code == 404:
            raise SnapchatError(f"👻 Snapchat par `@{clean}` naam ka koi account nahi mila!")
        if resp.status_code != 200:
            raise SnapchatError(f"Snapchat server response HTTP {resp.status_code}. Thodi der baad try karein.")

        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', resp.text, re.S)
        if m:
            nd = json.loads(m.group(1))
            pp = ((nd.get("props") or {}).get("pageProps")) or {}
        else:
            # Snapchat's newer App Router embeds React Flight chunks instead of __NEXT_DATA__.
            pp = {}
            public_profile = None
            for match in re.finditer(r'self\.__next_f\.push\((.*?)\)</script>', resp.text, re.S):
                try:
                    chunk = json.loads(match.group(1))
                    text = chunk[1] if len(chunk) > 1 and isinstance(chunk[1], str) else ""
                    for hit in re.finditer(r'"publicProfile"\s*:\s*(?=\{)', text):
                        candidate, _ = json.JSONDecoder().raw_decode(text[hit.end():])
                        if isinstance(candidate, dict) and str(candidate.get("username", "")).lower() == clean:
                            public_profile = candidate
                            break
                    if public_profile:
                        break
                except (ValueError, TypeError, IndexError):
                    continue
            if public_profile:
                pp["userProfile"] = {"$case": "publicProfileInfo", "publicProfileInfo": public_profile}
            else:
                # Simplified public web profiles expose the avatar in structured data.
                for match in re.finditer(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', resp.text, re.S | re.I):
                    try:
                        obj = json.loads(htmllib.unescape(match.group(1)))
                        obj = obj.get("mainEntity", obj) if isinstance(obj, dict) else {}
                        if str(obj.get("alternateName", "")).lstrip("@").lower() != clean:
                            continue
                        image = obj.get("image") if isinstance(obj.get("image"), str) else ""
                        pp["userProfile"] = {"$case": "userInfo", "userInfo": {
                            "username": clean, "displayName": obj.get("name") or clean,
                            "bitmoji3d": {"avatarImage": {"url": image}} if image else {},
                        }}
                        break
                    except (ValueError, TypeError, AttributeError):
                        continue
            if not pp.get("userProfile"):
                raise SnapchatError(f"👻 `@{clean}` ka public profile data Snapchat se nahi mila. Username/link check karein ya baad mein try karein.")
        up = pp.get("userProfile") or {}
        case_type = up.get("$case")

        if not up or (not up.get("publicProfileInfo") and not up.get("userInfo")):
            raise SnapchatError(f"👻 Snapchat par `@{clean}` naam ka koi active account nahi mila!")

        stories = _parse_snap_list((pp.get("story") or {}).get("snapList"))

        highlights: List[SnapHighlight] = []
        for idx, hl in enumerate(pp.get("curatedHighlights") or []):
            if not isinstance(hl, dict):
                continue
            snaps = _parse_snap_list(hl.get("snapList"))
            if not snaps:
                continue
            t_obj = hl.get("storyTitle")
            title = str(t_obj.get("value") if isinstance(t_obj, dict) else (t_obj or "")).strip()
            if not title:
                title = f"Highlight #{idx + 1}"
            hid_obj = hl.get("highlightId") or hl.get("storyId")
            hid = str(hid_obj.get("value") if isinstance(hid_obj, dict) else (hid_obj or idx))
            highlights.append(
                SnapHighlight(index=len(highlights), highlight_id=hid, title=title, snaps=snaps)
            )

        spotlights: List[MediaItem] = []
        for sp in (pp.get("spotlightHighlights") or []):
            if isinstance(sp, dict):
                spotlights.extend(_parse_snap_list(sp.get("snapList")))

        front_url = ""
        bg_url = ""
        has_bitmoji = False

        def _extract_bitmoji_pair(b3: Any) -> Tuple[str, str]:
            if not isinstance(b3, dict):
                return ("", "")
            av_obj = b3.get("avatarImage") or {}
            bg_obj = b3.get("backgroundImage") or {}
            # Prefer fallbackUrl (`._FMpng`) so Telegram renders crisp lossless PNG instead of raw WebP
            f_u = str(av_obj.get("fallbackUrl") or av_obj.get("url") or "").strip()
            b_u = str(bg_obj.get("fallbackUrl") or bg_obj.get("url") or "").strip()
            return (f_u, b_u)

        if case_type == "publicProfileInfo" or up.get("publicProfileInfo"):
            pinfo = up.get("publicProfileInfo") or {}
            uname = str(pinfo.get("username") or clean).lower()
            title = str(pinfo.get("title") or uname).strip()
            bio = str(pinfo.get("bio") or "").strip()
            subs_raw = pinfo.get("subscriberCount")
            subs = int(subs_raw) if str(subs_raw or "").isdigit() else None
            verified = pinfo.get("badge") in (1, "star")
            web = str(pinfo.get("websiteUrl") or "").strip()
            addr = str(pinfo.get("address") or "").strip()

            raw_front = str(pinfo.get("profilePictureUrl") or "").strip()
            raw_bg = str(pinfo.get("squareHeroImageUrl") or pinfo.get("heroImageUrl") or "").strip()
            b_front, b_bg = _extract_bitmoji_pair(pinfo.get("bitmoji3d"))
            if b_front or b_bg:
                has_bitmoji = True

            front_url = _upgrade_snap_avatar_to_1080p(raw_front) if raw_front else b_front
            bg_url = _upgrade_snap_avatar_to_1080p(raw_bg) if raw_bg else b_bg

            # Avoid sending identical image twice if front & background point to same base asset
            if front_url and bg_url and front_url.split("._RS")[0] == bg_url.split("._RS")[0]:
                bg_url = ""

            # If front was empty but bg existed, promote bg to front
            if not front_url and bg_url:
                front_url, bg_url = bg_url, ""
            is_creator = True
        else:
            uinfo = up.get("userInfo") or {}
            uname = str(uinfo.get("username") or clean).lower()
            title = str(uinfo.get("displayName") or uname).strip()
            bio = ""
            subs = None
            verified = False
            web = ""
            addr = ""
            b_front, b_bg = _extract_bitmoji_pair(uinfo.get("bitmoji3d"))
            if b_front or b_bg:
                has_bitmoji = True
            front_url = b_front
            bg_url = b_bg if (b_bg and b_bg != b_front) else ""
            if not front_url and bg_url:
                front_url, bg_url = bg_url, ""
            is_creator = False

        snapcode_png_url = (
            f"https://app.snapchat.com/web/deeplink/snapcode?username={uname}&type=PNG&size=1000"
        )

        # 1. Download Front Profile Pic (or 1000x1000 PNG Snapcode fallback)
        avatar_bytes = b""
        chosen_front_url = front_url or snapcode_png_url
        for candidate_url in ([front_url, snapcode_png_url] if front_url else [snapcode_png_url]):
            try:
                img_r = sess.get(candidate_url, timeout=12)
                if img_r.status_code == 200 and len(img_r.content) > 500:
                    avatar_bytes = img_r.content
                    chosen_front_url = candidate_url
                    break
            except Exception:
                continue

        if not avatar_bytes:
            raise SnapchatError(f"@{uname} ki Snapchat image download nahi ho payi.")

        # 2. Download Background / Hero Cover Pic (if available!)
        background_bytes: Optional[bytes] = None
        chosen_bg_url = ""
        if bg_url:
            try:
                bg_r = sess.get(bg_url, timeout=14)
                if bg_r.status_code == 200 and len(bg_r.content) > 500 and bg_r.content != avatar_bytes:
                    background_bytes = bg_r.content
                    chosen_bg_url = bg_url
            except Exception:
                background_bytes = None

        result = SnapProfileResult(
            username=uname,
            display_name=title,
            bio=bio,
            subscribers=subs,
            is_verified=verified,
            website_url=web,
            address=addr,
            is_public_creator=is_creator,
            avatar_url=chosen_front_url,
            avatar_bytes=avatar_bytes,
            background_url=chosen_bg_url,
            background_bytes=background_bytes,
            snapcode_url=snapcode_png_url,
            has_bitmoji_3d=has_bitmoji,
            stories=stories,
            highlights=highlights,
            spotlights=spotlights,
        )
        self._cache[clean] = (now, result)
        self._cache[uname] = (now, result)
        return result

    def download_snapcode_bytes(self, username: str) -> bytes:
        uname = username.strip().lstrip("@").lower()
        url = f"https://app.snapchat.com/web/deeplink/snapcode?username={uname}&type=PNG&size=1000"
        r = self._new_session().get(url, timeout=12)
        if r.status_code == 200 and len(r.content) > 500:
            return r.content
        raise SnapchatError(f"@{uname} ka Snapcode download nahi ho paya.")

    def download_share_or_spotlight_url(self, url: str) -> Tuple[str, List[MediaItem]]:
        """Resolve a direct Snapchat Spotlight / Story share URL (`snapchat.com/spotlight/...` or `t.snapchat.com/...`)."""
        sess = self._new_session()
        resp = sess.get(url, allow_redirects=True, timeout=15)
        if resp.status_code != 200:
            raise SnapchatError("Ye Snapchat link open nahi ho paya.")

        # If redirect landed on a profile URL (@username or /add/username), handle as story/profile
        final_url = resp.url or url
        m_prof = re.search(r"snapchat\.com/(?:@|add/)([A-Za-z0-9._-]+)", final_url)
        if m_prof and "/spotlight/" not in final_url:
            prof = self.get_profile(m_prof.group(1))
            if prof.stories:
                return (f"👻 @{prof.username} — Snapchat Story", prof.stories)
            raise SnapchatError(f"@{prof.username} ki abhi koi active Public Snapchat Story nahi hai.")

        # Check __NEXT_DATA__ first if present
        m_nd = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', resp.text, re.S)
        if m_nd:
            pp = ((json.loads(m_nd.group(1)).get("props") or {}).get("pageProps")) or {}
            snaps = _parse_snap_list((pp.get("story") or {}).get("snapList"))
            if snaps:
                return ("👻 Snapchat Media", snaps)

        # Check OpenGraph og:video / JSON-LD contentUrl (App Router Spotlight pages)
        og_title_m = re.search(r'property="og:title"[^>]*content="([^"]+)"', resp.text) or re.search(
            r'content="([^"]+)"[^>]*property="og:title"', resp.text
        )
        title = htmllib.unescape(og_title_m.group(1)).strip() if og_title_m else "🔥 Snapchat Spotlight"

        og_vid_m = re.search(r'property="og:video"[^>]*content="([^"]+)"', resp.text) or re.search(
            r'content="([^"]+)"[^>]*property="og:video"', resp.text
        )
        if og_vid_m:
            v_url = htmllib.unescape(og_vid_m.group(1)).strip()
            if v_url.startswith("http"):
                return (title, [MediaItem(is_video=True, media_url=v_url)])

        content_url_m = re.search(r'"contentUrl"\s*:\s*"(https://[^"]+)"', resp.text)
        if content_url_m:
            v_url = htmllib.unescape(content_url_m.group(1).replace("\\u0026", "&").replace("\\/", "/")).strip()
            return (title, [MediaItem(is_video=True, media_url=v_url)])

        # Fallback: direct MP4 (`.27.` / `.80.` / `.111.`) before `upNext` inside RSC payload
        norm = resp.text.split('"upNext"', 1)[0].replace("\\u0026", "&").replace("\\/", "/")
        cdn_vids = re.findall(r"https://(?:cf-st|bolt-gcdn)\.sc-cdn\.net/(?:d|n)/[A-Za-z0-9_-]+\.(?:27|80|111)\.[A-Za-z0-9._?=&%-]+", norm)
        for cu in cdn_vids:
            clean_u = htmllib.unescape(cu)
            return (title, [MediaItem(is_video=True, media_url=clean_u)])

        raise SnapchatError("Ye Snapchat Spotlight/Story link expire ho chuka hai ya private hai.")
