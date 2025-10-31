#!/usr/bin/env python3
"""
apify_key_rotator.py
- Polls Apify limits for each stored token
- Updates DB usage values
- Disables active key at >=99%
- Activates lowest-usage key
- If active >=90% and others >=100% → send alert email
- Logs all actions to apify_rotation_logs
"""

import os
import json
import time
import requests
import traceback
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from db import get_connection
from dotenv import load_dotenv

# ---------- Configuration ----------
APIFY_LIMITS_URL = "https://api.apify.com/v2/users/me/limits?token={token}"
LOCK_NAME = "apify_key_rotate_lock"
LOCK_TIMEOUT = 5
ALERT_THRESHOLD_ACTIVE = 0.90  # 90%
DISABLE_THRESHOLD = 0.99       # 99%
load_dotenv()

# SMTP
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
ALERT_EMAIL_TO = os.getenv("ALERT_EMAIL_TO")

# ---------- Logging helper ----------
def append_log(conn, action, message, meta=None):
    """Insert rotation log entry (fails silently if DB locked)."""
    try:
        with conn.cursor(dictionary=True, buffered=True) as cur:
            cur.execute(
                "INSERT INTO apify_rotation_logs (action, message, meta) VALUES (%s, %s, %s)",
                (action, message, json.dumps(meta) if meta else None)
            )
        conn.commit()
    except Exception as e:
        print("⚠️ Failed to append log:", e)

# ---------- Email helper ----------
def send_email_alert(subject: str, body_text: str):
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS and ALERT_EMAIL_TO):
        print("SMTP config missing; cannot send alert.")
        return False

    recipients = [r.strip() for r in ALERT_EMAIL_TO.split(",") if r.strip()]
    msg = MIMEMultipart()
    msg["From"] = SMTP_USER
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.attach(MIMEText(body_text, "plain"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, recipients, msg.as_string())
        print("✅ Alert sent to:", recipients)
        return True
    except Exception as e:
        print("❌ Email send failed:", e)
        return False

# ---------- Apify usage fetch ----------
def fetch_apify_usage(token: str, timeout=12):
    """Return dict with current_balance, max_limit, start, end."""
    url = APIFY_LIMITS_URL.format(token=token)
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    j = r.json().get("data", {})
    current_usd = float(j.get("current", {}).get("monthlyUsageUsd") or 0.0)
    max_usd = float(j.get("limits", {}).get("maxMonthlyUsageUsd") or 0.0)

    def parse_iso(t):
        if not t:
            return None
        return datetime.fromisoformat(t.replace("Z", "+00:00"))

    return {
        "current_balance": round(current_usd, 4),
        "max_limit": round(max_usd, 4),
        "start": parse_iso(j.get("monthlyUsageCycle", {}).get("startAt")),
        "end": parse_iso(j.get("monthlyUsageCycle", {}).get("endAt")),
        "raw": j
    }

# ---------- Core rotation ----------
def rotate_apify_keys():
    conn = get_connection()
    if not conn:
        print("❌ DB connection failed")
        return

    try:
        with conn.cursor(dictionary=True, buffered=True) as cur:
            # Acquire lock
            cur.execute("SELECT GET_LOCK(%s, %s) AS got", (LOCK_NAME, LOCK_TIMEOUT))
            row = cur.fetchone()
            if not row or row["got"] != 1:
                raise RuntimeError("Could not acquire rotation lock")

            # Fetch all keys
            cur.execute("SELECT * FROM apify_keys ORDER BY priority ASC, id ASC")
            keys = cur.fetchall()
            if not keys:
                raise RuntimeError("No keys found")

        usage_info = {}
        best_key = None
        best_ratio = 999
        exhausted = []

        for k in keys:
            try:
                info = fetch_apify_usage(k["token"])
                ratio = (info["current_balance"] / info["max_limit"]) if info["max_limit"] else 1.0
                usage_info[k["id"]] = info

                with conn.cursor(dictionary=True, buffered=True) as cur:
                    cur.execute("""
                        UPDATE apify_keys
                        SET current_balance=%s, max_amount_limit=%s,
                            monthly_start_at=%s, monthly_end_at=%s,
                            last_checked_at=UTC_TIMESTAMP(),
                            last_checked_raw=%s, modified_at=NOW()
                        WHERE id=%s
                    """, (info["current_balance"], info["max_limit"], info["start"], info["end"], json.dumps(info["raw"]), k["id"]))
                conn.commit()

                if ratio >= 1.0:
                    exhausted.append(k["user_name"])

                if not k["is_disabled"] and ratio < best_ratio:
                    best_ratio = ratio
                    best_key = k

            except Exception as e:
                append_log(conn, "fetch_error", f"Fetch failed for key {k['id']}: {e}")
                continue

        # Check active key
        with conn.cursor(dictionary=True, buffered=True) as cur:
            cur.execute("SELECT * FROM apify_keys WHERE is_active=1 LIMIT 1")
            active = cur.fetchone()

        if active:
            ar = usage_info.get(active["id"])
            active_ratio = (ar["current_balance"] / ar["max_limit"]) if (ar and ar["max_limit"]) else 1.0
            if active_ratio >= DISABLE_THRESHOLD:
                disabled_until = ar.get("end") if ar else None
                with conn.cursor(dictionary=True, buffered=True) as cur:
                    cur.execute("""
                        UPDATE apify_keys
                        SET is_disabled=1, is_active=0, disabled_until=%s
                        WHERE id=%s
                    """, (disabled_until, active["id"]))
                conn.commit()
                append_log(conn, "disable_active",
                           f"Disabled key {active['id']} (≥99%)",
                           {"id": active["id"], "ratio": active_ratio})
                print(f"🔒 Disabled active key {active['id']} ({active_ratio*100:.2f}%)")

        # Activate lowest-usage key
        chosen_id = None
        if best_key:
            with conn.cursor(dictionary=True, buffered=True) as cur:
                cur.execute("SELECT is_disabled FROM apify_keys WHERE id=%s", (best_key["id"],))
                b = cur.fetchone()
                if not b or not b["is_disabled"]:
                    chosen_id = best_key["id"]

            if chosen_id:
                with conn.cursor(dictionary=True, buffered=True) as cur:
                    cur.execute("UPDATE apify_keys SET is_active=0")
                    cur.execute("UPDATE apify_keys SET is_active=1, last_used_at=UTC_TIMESTAMP() WHERE id=%s", (chosen_id,))
                conn.commit()
                append_log(conn, "activate_key", f"Activated key id={chosen_id}")
                print(f"✅ Activated key id={chosen_id}")
            else:
                append_log(conn, "no_key_activated", "No eligible key found")
                print("⚠️ No eligible key to activate")

        # Check alert condition
        with conn.cursor(dictionary=True, buffered=True) as cur:
            cur.execute("SELECT * FROM apify_keys")
            rows = cur.fetchall()

        active = next((r for r in rows if r["is_active"]), None)
        others = [r for r in rows if not r["is_active"]]

        if active:
            act_ratio = (active["current_balance"] / active["max_amount_limit"]) if active["max_amount_limit"] else 1.0
            all_others_exhausted = all(
                (o["is_disabled"] or
                 (o["current_balance"] / o["max_amount_limit"] if o["max_amount_limit"] else 1.0) >= 1.0)
                for o in others
            )

            if act_ratio >= ALERT_THRESHOLD_ACTIVE and all_others_exhausted:
                subject = "🚨 All Apify keys exhausted / active key near limit"
                body = (
                    f"Active key: {active['user_name']} (id={active['id']})\n"
                    f"Usage: {active['current_balance']} / {active['max_amount_limit']} "
                    f"({act_ratio*100:.2f}%)\n\n"
                    "All other keys are exhausted or disabled.\n"
                    "Please add a new Apify key or increase quota.\n\n"
                    f"Time (UTC): {datetime.now(timezone.utc).isoformat()}"
                )
                sent = send_email_alert(subject, body)
                append_log(conn, "alert_sent" if sent else "alert_failed", body)
                print("📧 Alert email sent" if sent else "⚠️ Alert send failed")

        # Release lock
        with conn.cursor(dictionary=True, buffered=True) as cur:
            cur.execute("SELECT RELEASE_LOCK(%s)", (LOCK_NAME,))
        conn.commit()

    except Exception as e:
        print("❌ Rotation error:", e)
        append_log(conn, "rotation_error", str(e))
    finally:
        try:
            conn.close()
        except Exception:
            pass

# ---------- Main ----------
if __name__ == "__main__":
    rotate_apify_keys()
