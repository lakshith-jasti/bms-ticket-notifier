
import os
import json
import time
import base64
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

# ==================================================
# CONFIGURATION
# ==================================================

URL = (
    "https://in.bookmyshow.com/movies/hyderabad/"
    "avengers-doomsday/buytickets/ET00526306/20261217"
    "?etCodes=*&language=english&refEventCode=ET00526306"
)

PRASADS_NAME = "Prasads Multiplex"
ALLU_NAME = "ALLU Cinemas: Kokapet"

CHECK_INTERVAL = 60
COOLDOWN_SECONDS = 300

START_HOUR = 8
END_HOUR = 23

TIMEZONE = ZoneInfo("Asia/Kolkata")

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_TO_EMAIL = os.getenv("RESEND_TO_EMAIL", "")
RESEND_FROM_EMAIL = os.getenv(
    "RESEND_FROM_EMAIL",
    "onboarding@resend.dev"
)

STATE_FILE = Path("bms_state.json")
SCREENSHOT_DIR = Path("alerts")


# ==================================================
# STATE
# ==================================================

def load_state():
    try:
        state = json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        )

        state.setdefault("prasads_alerted", False)
        state.setdefault("allu_green_alerted", False)

        return state

    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "prasads_alerted": False,
            "allu_green_alerted": False
        }


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, indent=2),
        encoding="utf-8"
    )


# ==================================================
# MONITORING HOURS
# ==================================================

def within_monitoring_hours():
    now = datetime.now(TIMEZONE)
    return START_HOUR <= now.hour < END_HOUR


def seconds_until_start():
    now = datetime.now(TIMEZONE)

    next_start = now.replace(
        hour=START_HOUR,
        minute=0,
        second=0,
        microsecond=0
    )

    if now.hour >= END_HOUR:
        next_start += timedelta(days=1)

    return max(1, int((next_start - now).total_seconds()))


# ==================================================
# GREEN COLOUR DETECTION
# ==================================================

def is_green(border_color):
    if not border_color:
        return False

    match = re.search(
        r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)",
        border_color
    )

    if not match:
        return False

    red, green, blue = map(int, match.groups())

    return (
        green >= 100
        and green > red * 1.15
        and green > blue * 1.15
    )


# ==================================================
# PRASADS DETECTION
# ==================================================

def check_prasads(body):
    return PRASADS_NAME.casefold() in body.casefold()


# ==================================================
# ALLU DOLBY CINEMA DETECTION
# ==================================================

def check_allu_dolby(page):
    """
    Inspect Dolby Cinema showtime buttons belonging
    to ALLU Cinemas: Kokapet only.

    Laser 4K Atmos buttons are excluded because
    their aria-label does not contain DOLBY CINEMA.
    """

    buttons = page.locator(
        '[role="button"][aria-label*="DOLBY CINEMA" i]'
    )

    results = []

    for index in range(buttons.count()):
        button = buttons.nth(index)

        result = button.evaluate(
            """
            button => {
                let element = button;

                for (let i = 0; i < 12 && element; i++) {
                    const text = element.innerText || "";

                    if (
                        text.includes("ALLU Cinemas: Kokapet") &&
                        text.toUpperCase().includes("DOLBY CINEMA")
                    ) {
                        return {
                            found: true,
                            label: button.getAttribute("aria-label"),
                            borderColor:
                                getComputedStyle(button).borderColor
                        };
                    }

                    element = element.parentElement;
                }

                return { found: false };
            }
            """
        )

        if result.get("found"):
            results.append(result)

    return results


# ==================================================
# EMAIL
# ==================================================

def send_email(alert_messages, screenshot_path):

    if not RESEND_API_KEY or not RESEND_TO_EMAIL:
        print("  Email settings missing.")
        return False

    message = "\n".join(
        f"- {item}" for item in alert_messages
    )

    image_data = base64.b64encode(
        screenshot_path.read_bytes()
    ).decode("utf-8")

    payload = {
        "from": RESEND_FROM_EMAIL,
        "to": [RESEND_TO_EMAIL],
        "subject": "BMS Alert: Avengers Doomsday",
        "text": (
            "New BookMyShow alert!\n\n"
            f"{message}\n\n"
            "Date: 17 December 2026\n"
            "Open BookMyShow to check showtimes and seats."
        ),
        "attachments": [
            {
                "filename": screenshot_path.name,
                "content": image_data
            }
        ]
    }

    try:
        response = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json"
            },
            json=payload,
            timeout=30
        )

        if response.status_code in (200, 201):
            print("  Email sent successfully.")
            return True

        print(f"  Email failed: HTTP {response.status_code}")
        print(response.text[:300])
        return False

    except requests.RequestException as error:
        print(f"  Email error: {error}")
        return False


# ==================================================
# MAIN CHECK
# ==================================================

def check_bookmyshow(page, state):

    now = datetime.now(TIMEZONE)

    print(
        f"\n[{now.strftime('%d-%m-%Y %H:%M:%S')}] "
        "Checking BookMyShow..."
    )

    response = page.goto(
        URL,
        wait_until="domcontentloaded",
        timeout=60000
    )

    if response is None or response.status != 200:
        status = response.status if response else "No response"
        print(f"  Request failed: {status}")
        return state, True

    page.wait_for_timeout(7000)

    if "Attention Required" in page.title():
        print("  Cloudflare/access block detected.")
        return state, True

    body = page.locator("body").inner_text(timeout=15000)

    if "Oops! Something went wrong" in body:
        print("  BookMyShow page error.")
        return state, True

    alerts = []

    # ----------------------------------------------
    # 1. PRASADS
    # ----------------------------------------------

    prasads_found = check_prasads(body)

    if prasads_found:
        print("  Prasads Multiplex: FOUND")

        if not state["prasads_alerted"]:
            alerts.append(
                "Prasads Multiplex has appeared on BookMyShow."
            )
    else:
        print("  Prasads Multiplex: Not listed")

        state["prasads_alerted"] = False

    # ----------------------------------------------
    # 2. ALLU DOLBY CINEMA
    # ----------------------------------------------

    dolby_results = check_allu_dolby(page)

    allu_green = False

    if not dolby_results:
        print("  ALLU Dolby Cinema showtimes: Not found")

    else:
        for show in dolby_results:
            color = show["borderColor"]
            green = is_green(color)

            print(
                f"  ALLU {show['label']} | "
                f"Border: {color}"
            )

            if green:
                allu_green = True

        if allu_green:
            print("  ALLU Dolby Cinema: GREEN")
        else:
            print("  ALLU Dolby Cinema: NOT GREEN")

    if allu_green and not state["allu_green_alerted"]:
        alerts.append(
            "ALLU Cinemas: Kokapet - "
            "A Dolby Cinema showtime has turned GREEN!"
        )

    if dolby_results and not allu_green:
        state["allu_green_alerted"] = False

    # ----------------------------------------------
    # 3. EMAIL NEW ALERTS
    # ----------------------------------------------

    if alerts:

        print("  NEW ALERT DETECTED!")

        SCREENSHOT_DIR.mkdir(exist_ok=True)

        timestamp = now.strftime("%Y%m%d_%H%M%S")

        screenshot_path = (
            SCREENSHOT_DIR / f"bms_alert_{timestamp}.png"
        )

        page.screenshot(
            path=str(screenshot_path),
            full_page=True
        )

        print("  Screenshot captured.")

        if send_email(alerts, screenshot_path):

            if prasads_found:
                state["prasads_alerted"] = True

            if allu_green:
                state["allu_green_alerted"] = True

    save_state(state)

    return state, False


# ==================================================
# MAIN
# ==================================================


def main():

    print("=" * 55)
    print("AVENGERS: DOOMSDAY MONITOR")
    print("=" * 55)

    print("\nMonitoring:")
    print("  1. Prasads Multiplex - Theatre appearance")
    print("  2. ALLU Cinemas - Dolby Cinema turns green")

    print("\nCheck interval: 1 minute")
    print("Browser: Restarted after every check")
    print("Monitoring hours: 8 AM - 11 PM IST")
    print("Press CTRL+C to stop.")

    state = load_state()

    try:
        while True:

            if not within_monitoring_hours():
                wait = seconds_until_start()
                print("\nOutside monitoring hours. Waiting until 8 AM.")
                time.sleep(wait)
                continue

            try:
                with sync_playwright() as p:

                    browser = p.chromium.launch(headless=False)

                    try:
                        page = browser.new_page(
                            viewport={
                                "width": 1440,
                                "height": 1000
                            }
                        )

                        state, access_error = check_bookmyshow(
                            page, state
                        )

                    finally:
                        browser.close()
                        print("  Browser closed.")

                if access_error:
                    print("  Access error. Cooling down for 30 minutes.")
                    time.sleep(COOLDOWN_SECONDS)
                    continue

            except Exception as error:
                print(f"  Check failed: {error}")
                print("  Cooling down for 30 minutes.")
                time.sleep(COOLDOWN_SECONDS)
                continue

            print("Next check in 1 minute.")
            time.sleep(CHECK_INTERVAL)

    except KeyboardInterrupt:
        print("\nMonitor stopped.")

if __name__ == "__main__":
    main()
