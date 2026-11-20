# 🚀 Free Tier Scaling & Rate-Limit Guide for PeakCut

This guide explains how to get the absolute maximum daily upload volume from YouTube, Instagram, and TikTok completely within the **Free Tier ($0)**, and details the built-in rate-limit protection in PeakCut.

---

## 1. Built-in Free Tier Protections (Active in PeakCut)

PeakCut includes automated guards to protect your accounts from being restricted or spam-flagged:

* **Smart Staggering**:
  * **Reel vs. Story**: Stories are automatically delayed by 5 minutes after a Reel finishes so Meta Graph API does not process concurrent video containers.
  * **Multi-Clip Spacing**: When multiple clips are generated from a single video/stream, subsequent clips are automatically spaced by 20 minutes instead of hitting the platforms simultaneously.
* **Meta Rolling Rate-Limit Shield (`#4` Error)**:
  * If Instagram returns a temporary hourly rate limit (`(#4) Application request limit reached`), PeakCut enters an automatic **20-minute cooldown** without burning through retry attempts.
* **YouTube Daily Quota Shield (`uploadLimitExceeded` / `429`)**:
  * When YouTube's daily upload quota metric is reached, PeakCut automatically schedules remaining clips for the daily quota reset (**12:00 AM PST / 1:30 PM IST**) instead of marking them failed.

---

## 2. Increasing YouTube Quota to 30–60+ Videos/Day for Free

### Option A: Official Free Quota Extension (Recommended)
1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Select your PeakCut GCP project.
3. Navigate to **APIs & Services** $\rightarrow$ **YouTube Data API v3** $\rightarrow$ **Quotas & System Limits**.
4. Filter for `Queries per day` or `Video Uploads per day`.
5. Click **Edit Quotas** / **Request Quota Increase** and request **50,000 to 100,000 units/day**.
6. In the justification, explain: *"Automated gaming highlights creator for personal YouTube channel"*. Google typically approves quota extensions for active channels within 24–48 hours at zero cost.

### Option B: Multi-Project Quota Pooling ($0 Instant 2x Scale)
* Create a second Google Cloud Project (e.g. `PeakCut-Uploader-2`).
* Enable YouTube Data API v3 on Project 2 to get a second **10,000 units/day** allocation.
* Connect a second channel or alternate credentials to double your daily upload capacity to 12+ videos per day.

---

## 3. Increasing Instagram Volume to 50+ Reels/Day for Free

* **Add Secondary Accounts**:
  * In Instagram, you can manage up to 5 business/creator accounts in the same app.
  * In PeakCut, connect your secondary accounts (e.g., `@willweearn` and `@willweearn_clips`).
  * Each connected Instagram account receives its own **25 Reels/day** API allotment.
  * With 2 accounts, you can publish **50 Reels/day** across your network without paying anything.

---

## 4. Viral Growth Strategy (Quality > Quantity)

Algorithm testing proves that uploading 2–4 high-retention clips (Score $\ge 85$) spaced 2–3 hours apart performs significantly better than dumping 15 clips at once. Use PeakCut's score filters to publish only top viral moments.
