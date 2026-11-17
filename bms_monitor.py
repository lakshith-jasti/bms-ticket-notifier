
import os
import json
import time
import base64
from datetime import datetime
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
# STATE MANAGEMENT
# ==================================================

def load_state():
    try:
        state = json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        )

        state.setdefault("prasads_alerted", False)
        state.setdefault("allu_dolby_alerted", False)

        return state

    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "prasads_alerted": False,
            "allu_dolby_alerted": False
        }


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, indent=2),
        encoding="utf-8"
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
    Detect Dolby Cinema showtime buttons belonging
    to ALLU Cinemas: Kokapet.

    Colour and availability are ignored.
    Laser 4K Atmos is not included.
    """

    buttons = page.locator(
        '[role="button"][aria-label*="DOLBY CINEMA" i]'
    )

    results = []

    for index in range(buttons.count()):
        button = buttons.nth(index)

        result = button.evaluate(
            """
            (button, theatreName) => {
                let element = button;

                for (let i = 0; i < 12 && element; i++) {
                    const text = element.innerText || "";

                    if (
                        text.toLowerCase().includes(
                            theatreName.toLowerCase()
                        ) &&
                        text.toUpperCase().includes("DOLBY CINEMA")
                    ) {
                        return {
                            found: true,
                            label: button.getAttribute("aria-label")
                        };
                    }

                    element = element.parentElement;
                }

                return { found: false };
            }
            """,
            ALLU_NAME
        )

        if result.get("found"):
            results.append(result)

    return results


# ==================================================
# EMAIL NOTIFICATION
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
# BOOKMYSHOW CHECK
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
    # 1. PRASADS THEATRE APPEARANCE
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
    # 2. ALLU DOLBY CINEMA APPEARANCE
    # ----------------------------------------------

    dolby_results = check_allu_dolby(page)

    if dolby_results:

        for show in dolby_results:
            print(
                f"  ALLU Dolby Cinema detected: "
                f"{show['label']}"
            )

        if not state["allu_dolby_alerted"]:
            alerts.append(
                "ALLU Cinemas: Kokapet - "
                "A Dolby Cinema showtime has appeared "
                "on BookMyShow."
            )

    else:

        print("  ALLU Dolby Cinema showtimes: Not found")
        state["allu_dolby_alerted"] = False

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

            if dolby_results:
                state["allu_dolby_alerted"] = True

    save_state(state)

    return state, False


# ==================================================
# MAIN MONITORING LOOP - 24/7
# ==================================================

def main():

    print("=" * 55)
    print("AVENGERS: DOOMSDAY MONITOR")
    print("=" * 55)

    print("\nMonitoring:")
    print("  1. Prasads Multiplex - Theatre appearance")
    print("  2. ALLU Cinemas: Kokapet - Dolby Cinema appearance")

    print("\nMonitoring:")
    print("  1. Prasads Multiplex - Theatre appearance")
    print("  2. ALLU Cinemas: Kokapet - Dolby Cinema showtime")
    print("\nCheck interval: 60 seconds")
    print("Monitoring: 24/7")

    state = load_state()

    try:

        while True:

            try:

                with sync_playwright() as p:

                    browser = p.chromium.launch(
                        headless=False
                    )

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

                    print(
                        "  Access error. "
                        "Cooling down for 5 minutes."
                    )

                    time.sleep(COOLDOWN_SECONDS)
                    continue

            except Exception as error:

                print(f"  Check failed: {error}")
                print("  Cooling down for 5 minutes.")

                time.sleep(COOLDOWN_SECONDS)
                continue

            print("Next check in 60 seconds.")
            time.sleep(CHECK_INTERVAL)

    except KeyboardInterrupt:

        print("\nMonitor stopped.")


if __name__ == "__main__":
    main()
