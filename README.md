# ⚡ SocialGrab HD — 3-in-1 Social Downloader Website + Clean Path API (Vercel Ready)

A modern, zero-database **Instagram + Snapchat + Telegram HD Media Downloader Web App & Clean Path-Based REST API**, architected natively for **Vercel** (`public/index.html` + `@vercel/python` FastAPI Serverless runtime).

---

## 📖 Clean Path-Based API Endpoints

Replace `{key}` with your API key (built-in keys: `demo`, `free`, `socialgrab`, or create custom keys in `👑 Admin` / Vercel `API_KEYS` env var):

### 👻 1. Snapchat Endpoints
| Standard Endpoint | 1-Word Shorthand | Description |
| :--- | :--- | :--- |
| `/{key}/snap/pic/{query}` | `/{key}/snappic/{query}` | **Front + Background HD Profile Pics** *(add `?direct=1` for raw binary)* |
| `/{key}/snap/info/{query}` | `/{key}/snapinfo/{query}` | Full Snapchat Profile Info + Subscribers + Bio + Dual HD Pics |
| `/{key}/snap/story/{query}` | `/{key}/snapstory/{query}` | Active 24-hour Snapchat Stories |
| `/{key}/snap/highlights/{query}` | `/{key}/snaphighlights/{query}` | All Snapchat Highlights + HD Snaps *(or `/{query}/{index}`)* |
| `/{key}/snap/spotlight/{query}` | `/{key}/snapspotlight/{query}` | Snapchat Spotlight HD MP4 *(username or Spotlight URL)* |
| `/{key}/snap/snapcode/{query}` | `/{key}/snapcode/{query}` | 1000×1000 PNG Snapcode QR |

### 📸 2. Instagram Endpoints
| Standard Endpoint | 1-Word Shorthand | Description |
| :--- | :--- | :--- |
| `/{key}/insta/pic/{query}` | `/{key}/instapic/{query}` | **1080p HD DP** *(Public, Private & 0-post accounts! Add `?direct=1` for JPG)* |
| `/{key}/insta/info/{query}` | `/{key}/instainfo/{query}` | Full Instagram Profile Stats + Bio + Badges + 1080p HD DP |
| `/{key}/insta/story/{query}` | `/{key}/instastory/{query}` | Active 24-hour Instagram Stories |
| `/{key}/insta/highlights/{query}` | `/{key}/instahighlights/{query}` | Highlights Trays *(or `/{username}/{highlight_id}` for HD slides)* |
| `/{key}/insta/post/{query}` | `/{key}/instapost/{query}` | Post / Multi-Slide Carousel *(shortcode, URL, or username)* |
| `/{key}/insta/reel/{query}` | `/{key}/instareel/{query}` | HD Reel Video with Audio *(shortcode, URL, or username)* |

### ✈️ 3. Telegram Endpoints
| Standard Endpoint | 1-Word Shorthand | Description |
| :--- | :--- | :--- |
| `/{key}/tg/pic/{query}` | `/{key}/tgpic/{query}` | Telegram User / Channel / Group HD Profile Pic *(+ `?direct=1`)* |
| `/{key}/tg/info/{query}` | `/{key}/tginfo/{query}` | Full Telegram Profile / Channel Info + Subscribers + Bio + HD DP |
| `/{key}/tg/story/{query}` | `/{key}/tgstory/{query}` | Active 24-hour Telegram Stories |
| `/{key}/tg/highlights/{query}` | `/{key}/tghighlights/{query}` | Telegram Pinned Story Highlights |
| `/{key}/tg/post/{query}` | `/{key}/tgpost/{query}` | Public Channel Media Post *(e.g. `telegram/459`, URL, or channel)* |

---

## 🚀 Deploy on Vercel

1. Push `social_downloader_web/` to GitHub (or run `npx vercel --prod` inside `social_downloader_web/`).
2. Import on **[vercel.com/new](https://vercel.com/new)** (Framework Preset: `Other` — auto-configured via `vercel.json`).
3. *(Optional)* Set Environment Variables:
   - `ADMIN_KEY=7707859083`
   - `API_KEYS=demo,mykey1,mykey2`
   - `TELEGRAM_BOT_TOKEN` & `ADMIN_IDS` *(forwards live searches to your Telegram chat)*
