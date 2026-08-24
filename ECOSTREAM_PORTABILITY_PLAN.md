# 🌐 ECOStream: StreamVault Multi-Device Portability & Architectural Blueprint

> **System Goal:** Transform StreamVault from a Windows desktop application into a universal, multi-device streaming ecosystem (**ECOStream**) capable of running seamlessly across **PC, Mobile (iOS/Android), and Smart TVs (Android TV, Fire TV, WebOS, Apple TV)** with adaptive interfaces (including a Netflix-grade 10-foot TV experience).

---

## 1. System Overview & Current State

StreamVault turns Telegram into an unlimited, zero-cost personal media CDN:
- **Storage & Ingestion:** Telegram MTProto (`Telethon`) & local Bot API server bridge.
- **Transcoding & Streaming:** Real-time chunk streaming with range requests, on-the-fly FFmpeg HLS segmentation, and VLC endpoints.
- **Metadata & Discovery:** SQLite FTS5 search index (`cache_fts.db`), TMDb/OMDb automated enrichment, and local poster/LQIP cache.
- **Current Limitation:** Tightly coupled Python backend with server-side rendered HTML (`render.py`) and Windows-specific `pywebview` / `launcher.pyw` container.

---

## 2. Technology Evaluation: Platform Matrix

| Strategy | PC / Mac / Linux | Mobile (iOS / Android) | TV (Android TV / Fire TV) | Maintainability | Verdict |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Electron Only** | ⭐️ Excellent | ❌ Not supported | ❌ Not supported | Single codebase (Desktop only) | **Fails mobile & TV requirements.** |
| **Pure Python (Kivy / BeeWare)** | ⚠️ Fair | ⚠️ Heavy bundle, battery drain | ❌ Complex D-Pad & TV store approval | Python-only | **High friction & poor native feel.** |
| **3 Separate Native Apps** (Swift, Kotlin, C#) | Native | Native | Native | ❌ High (3x duplicate code & bugs) | **Resource-intensive.** |
| **Decoupled Client-Server (Headless Core + Universal Adaptive Frontend)** | ⭐️ Native / PWA | ⭐️ Native / PWA | ⭐️ Native TV App / PWA | ⭐️ Single frontend + single backend | **Recommended Architecture (Jellyfin / Netflix Model).** |

---

## 3. The ECOStream Architectural Model

```
                          ┌──────────────────────────────────────────────┐
                          │         STREAMVAULT CORE (Headless)          │
                          │   • Python / aiohttp / MTProto / FFmpeg      │
                          │   • JSON REST API & WebSocket Realtime State │
                          │   • HLS (.m3u8) & Direct HTTP Video Engine   │
                          │   • ZeroConf / mDNS Local LAN Discovery      │
                          └──────────────────────┬───────────────────────┘
                                                 │
                                     HTTP / WebSocket API + HLS
                                                 │
            ┌────────────────────────────────────┼────────────────────────────────────┐
            ▼                                    ▼                                    ▼
┌───────────────────────┐            ┌───────────────────────┐            ┌───────────────────────┐
│     TV EXPERIENCE     │            │   MOBILE EXPERIENCE   │            │  DESKTOP EXPERIENCE   │
│   (10-Foot UI Mode)   │            │   (Touch & Gesture)   │            │   (Dense & Managed)   │
├───────────────────────┤            ├───────────────────────┤            ├───────────────────────┤
│ • Spatial Navigation  │            │ • Bottom Navigation   │            │ • Sidebar navigation  │
│   (D-Pad / Remote)    │            │ • Pull-to-refresh     │            │ • Multi-select batch  │
│ • Billboard Hero &    │            │ • Vertical Feed &     │            │ • VLC Deep Linking    │
│   Auto-trailers       │            │   Horizontal Trays    │            │ • Keyboard shortcuts  │
│ • Horizontal Carousels│            │ • Gesture Seek & PiP  │            │ • High-density grid   │
│ • Big 4K Typography   │            │ • Low Bandwidth Mode  │            │ • Window controls     │
└───────────────────────┘            └───────────────────────┘            └───────────────────────┘
                                ONE UNIFIED FRONTEND CODEBASE
                          (React / Solid / Svelte + Tailwind CSS)
```

---

## 4. Layer-by-Layer Specifications

### Layer A: Backend Core (Headless Engine)
* **API Standardization:** Convert all existing routes in `routes.py` and `render.py` into clean JSON REST endpoints:
  - `GET /api/v1/media` — Paginated media list with filters, sorting, and album categories.
  - `GET /api/v1/media/{id}` — Full metadata, duration, TMDb details, audio tracks, and subtitle tracks.
  - `GET /api/v1/albums` — Virtual albums, series, and collection index.
  - `GET /api/v1/search?q={query}` — SQLite FTS5 instant search.
  - `GET /api/v1/stream/{id}/playlist.m3u8` — Adaptive HLS stream.
  - `POST /api/v1/playback/progress` — Cross-device synchronization of watch state & resume timestamps.
* **LAN Auto-Discovery (mDNS / SSDP):**
  - Broadcast `_streamvault._tcp.local` so clients on the same Wi-Fi instantly discover and pair with the server without manual IP entry.
* **Multi-Host Flexibility:**
  - Can run on a home PC, NAS, Raspberry Pi, VPS, or Docker container.

---

## 5. Designing the Netflix-Style TV Mode (10-Foot UI)

TV navigation is fundamentally different from Mobile or PC:
1. **Spatial Navigation (D-Pad Control)**:
   * Uses a directional focus engine (e.g., `@noriginmedia/norigin-spatial-navigation` or `spatial-navigation-js`).
   * When the user presses **Up / Down / Left / Right** on the remote, focus smoothly jumps between poster cards with glow/zoom effects.
2. **Hero Billboard & Auto-Preview**:
   * Top 40% of the TV screen showcases the currently highlighted movie with high-res backdrop, plot summary, IMDb rating, and audio/resolution badges.
3. **Horizontal Infinite Carousels**:
   * Rows categorized by: *Continue Watching*, *Recently Added from Telegram*, *Action*, *TV Series*, *Uncategorized*.
4. **Native TV Media Player**:
   * Custom OSD (On-Screen Display) with simple playback controls, episode drawer, audio track selector, and subtitle toggle via remote buttons.

---

## 6. Adaptive UI Profiles Comparison

| Feature | Mobile (Phone) | Desktop (PC / Mac) | TV (10-Foot Interface) |
| :--- | :--- | :--- | :--- |
| **Navigation** | Bottom Tab Bar & Swipe Gestures | Left Sidebar & Breadcrumbs | Spatial D-Pad Focus Manager |
| **Grid / Layout** | 2-Column Grid / Vertical Feed | 5–7 Column Dense Grid | Horizontal Infinite Carousels + Hero |
| **Typography** | Compact (14–16px) | Standard (14–18px) | Extra Large 4K (22–36px) |
| **Interactions** | Tap, Long-press, Swipe | Mouse Hover, Right-click | Remote OK/Enter, Back, Arrows |
| **Video Controls** | Double-tap seek, PiP, Touch bar | Hover seek-preview, VLC link | Remote D-Pad OSD, Drawer menu |

---

## 7. Cross-Platform Packaging Strategy

| Target Platform | Packaging Tool | Distribution Format |
| :--- | :--- | :--- |
| **Android TV & Google TV** | Capacitor / React Native TV | `.apk` / Android App Bundle |
| **Android Phone & Tablet** | Capacitor / React Native | `.apk` (Google Play / Sideload) |
| **Apple iOS & iPadOS** | Capacitor / PWA | App Store / WebKit PWA |
| **Windows Desktop** | Tauri v2 / pywebview | `.msi` / `.exe` lightweight installer (< 10MB) |
| **Smart TV Browsers (LG WebOS, Samsung Tizen)** | Hosted PWA | TV Web App manifest |

---

## 8. Phased Implementation Roadmap

### Phase 1: API Decoupling & Modern REST Layer
- [ ] Audit `routes.py` and ensure all endpoints return clean JSON payloads alongside stream URLs.
- [ ] Implement standard auth token/session verification for multi-client access.
- [ ] Implement `playback_state` synchronization API across devices (resume playback anywhere).

### Phase 2: Universal Frontend Foundation
- [ ] Initialize modern client (Vite + React / Solid + Tailwind CSS) with responsive design tokens.
- [ ] Implement device detection provider (`useDeviceMode()` -> `tv | mobile | desktop`).
- [ ] Build core UI components: MediaCard, CarouselRow, HeroBillboard, ModalDrawer.

### Phase 3: 10-Foot TV Engine & Spatial Navigation
- [ ] Integrate D-Pad focus manager and active card zoom/glow animations.
- [ ] Build TV-specific OSD player with remote keybinds (`Play/Pause`, `Seek`, `Subtitles`).
- [ ] Test on Android TV / Fire TV emulator and physical TV.

### Phase 4: Mobile & Desktop Wrappers
- [ ] Configure Capacitor for Android Mobile & Android TV builds.
- [ ] Configure Tauri v2 for ultra-fast, lightweight Windows/macOS desktop builds.
- [ ] Set up mDNS discovery for zero-configuration LAN pairing.

---

*Generated for StreamVault ECOStream Architecture.*
