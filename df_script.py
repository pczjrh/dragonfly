#!/usr/bin/env python3

# updated to include 90 minute window after sunrise+2h for automatic relay opening, and added manual override support via manual_override.json file.

from dragonfly import DragonFlyController
import argparse
import datetime
from datetime import date, timedelta
from astral import LocationInfo
from astral.sun import sun
import pytz
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
import json

DEFAULT_SUNRISE_OPEN_WINDOW_MINUTES = 90


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("dragonfly_cron")
    logger.setLevel(logging.INFO)

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )

    log_file = Path(__file__).with_name("df_script.log")
    file_handler = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=5)
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    return logger


def get_sunrise_sunset_and_civil_night(city_name, region, latitude, longitude, tz_name, logger):
    try:
        location = LocationInfo(city_name, region, tz_name, latitude, longitude)
        today = date.today()
        s = sun(location.observer, date=today, tzinfo=pytz.timezone(tz_name))
        return s["sunrise"], s["sunset"], s["dusk"]
    except Exception:
        logger.exception("Error calculating sunrise/sunset/civil night.")
        return None, None, None


def set_relay_5(controller, desired_state: str, logger: logging.Logger, dry_run: bool):
    current_state = controller.get_relay_status(5)
    if current_state == desired_state:
        logger.info("Relay 5 already '%s'. No change.", desired_state)
        return

    if dry_run:
        logger.info("[DRY-RUN] Would set relay 5 from '%s' to '%s'.", current_state, desired_state)
        return

    logger.info("Setting relay 5 from '%s' to '%s'.", current_state, desired_state)
    controller.set_relay_state(5, desired_state)


def classify_sensor_state(sensor_value) -> str:
    try:
        return "open" if int(str(sensor_value).strip()) > 400 else "closed"
    except ValueError:
        return "unknown"


def is_manual_override_enabled(override_file: Path, logger: logging.Logger) -> bool:
    """
    manual_override.json format:
    {
      "manual_override": true,
      "reason": "Daytime maintenance",
      "expires_at": "2026-07-26T18:00:00+01:00"  # optional, ISO-8601
    }
    """
    if not override_file.exists():
        return False

    try:
        raw = override_file.read_text(encoding="utf-8").strip()
        if not raw:
            logger.warning("Override file is empty: %s (treating as disabled)", override_file)
            return False
        data = json.loads(raw)
    except Exception:
        logger.exception("Failed to parse override file: %s", override_file)
        return False

    enabled = bool(data.get("manual_override", False))
    if not enabled:
        logger.info("Manual override file present but disabled.")
        return False

    reason = data.get("reason", "No reason provided")
    expires_at_raw = data.get("expires_at")

    if not expires_at_raw:
        logger.warning("Manual override ENABLED (no expiry). Reason: %s", reason)
        return True

    try:
        # Support both "...+01:00" and "...Z"
        expires_at_text = str(expires_at_raw).replace("Z", "+00:00")
        expires_at = datetime.datetime.fromisoformat(expires_at_text)
        if expires_at.tzinfo is None:
            logger.warning(
                "Manual override expiry has no timezone; assuming UTC. expires_at=%s",
                expires_at_raw,
            )
            expires_at = expires_at.replace(tzinfo=datetime.timezone.utc)

        now_utc = datetime.datetime.now(datetime.timezone.utc)
        expires_utc = expires_at.astimezone(datetime.timezone.utc)

        if now_utc <= expires_utc:
            logger.warning(
                "Manual override ENABLED until %s. Reason: %s",
                expires_at.isoformat(),
                reason,
            )
            return True

        logger.info(
            "Manual override expired at %s. Continuing normal automation.",
            expires_at.isoformat(),
        )
        return False

    except Exception:
        logger.exception(
            "Invalid 'expires_at' in override file (%s). Keeping manual override ENABLED for safety.",
            expires_at_raw,
        )
        return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DragonFly cron control script")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Evaluate logic and log actions without changing relay states",
    )
    parser.add_argument(
        "--sunrise-open-window-minutes",
        type=int,
        default=DEFAULT_SUNRISE_OPEN_WINDOW_MINUTES,
        help=(
            "How long after sunrise+2h to allow automatic daytime relay opening "
            f"(default: {DEFAULT_SUNRISE_OPEN_WINDOW_MINUTES} minutes)"
        ),
    )
    args = parser.parse_args()

    if args.sunrise_open_window_minutes <= 0:
        parser.error("--sunrise-open-window-minutes must be a positive integer")

    logger = setup_logging()
    logger.info("Starting run%s.", " [DRY-RUN]" if args.dry_run else "")

    override_file = Path(__file__).with_name("manual_override.json")
    if is_manual_override_enabled(override_file, logger):
        logger.info("Exiting due to active manual override.")
        sys.exit(0)

    # Set Location
    city = "London"
    region = "England"
    lat = 53.5074
    lon = -1.333
    timezone = "Europe/London"

    sunrise, sunset, civil_night = get_sunrise_sunset_and_civil_night(
        city, region, lat, lon, timezone, logger
    )
    if not all([sunrise, sunset, civil_night]):
        logger.error("Missing sun-time values. Exiting.")
        sys.exit(1)

    tz = pytz.timezone(timezone)
    current_time = datetime.datetime.now(tz)
    sunrise_plus_2h = sunrise + timedelta(hours=2)
    sunrise_plus_2h_window_end = sunrise_plus_2h + timedelta(
        minutes=args.sunrise_open_window_minutes
    )

    logger.info(
        "Current=%s | Sunrise=%s | Sunrise+2h=%s | Sunrise+2hWindowEnd=%s | OpenWindowMinutes=%s | Sunset=%s | CivilNight=%s",
        current_time,
        sunrise,
        sunrise_plus_2h,
        sunrise_plus_2h_window_end,
        args.sunrise_open_window_minutes,
        sunset,
        civil_night,
    )

    try:
        # Daylight transition condition: only in a short window after sunrise+2h,
        # open relay 5 only when the equipment position sensor is safe.
        if sunrise_plus_2h <= current_time < sunrise_plus_2h_window_end:
            logger.info(
                "Within the automatic opening window (%s minutes after sunrise+2h). Evaluating relay 5 daytime behavior.",
                args.sunrise_open_window_minutes,
            )
            with DragonFlyController("192.168.0.111", 10000) as controller:
                relay_5_state = controller.get_relay_status(5)
                if relay_5_state != "closed":
                    logger.info("Relay 5 is already open in transition window. No action.")
                    sys.exit(0)

                sensor_1_status = controller.get_sensor_status(1)
                sensor_1_state = classify_sensor_state(sensor_1_status)
                if sensor_1_state != "open":
                    logger.warning(
                        "Relay 5 is closed, but NOT opening because sensor 1 is %s. "
                        "Equipment is not in the correct location (position sensor).",
                        sensor_1_state,
                    )
                    sys.exit(0)

                logger.info(
                    "Relay 5 is closed and sensor 1 is %s. Opening relay 5.",
                    sensor_1_state,
                )
                set_relay_5(controller, "open", logger, args.dry_run)
            sys.exit(0)

        if sunrise_plus_2h_window_end <= current_time < sunset:
            logger.info(
                "Outside the automatic opening window (%s minutes after sunrise+2h). Leaving relay 5 for manual daytime operation.",
                args.sunrise_open_window_minutes,
            )
            sys.exit(0)

        if current_time < sunset:
            logger.info("Before sunset (and before sunrise+2h). Full daylight behavior: no action.")
            sys.exit(0)

        if sunset <= current_time < civil_night:
            logger.info("After sunset but before civil night. No action.")
            sys.exit(0)

        if current_time >= civil_night:
            logger.info("After civil night. Running imaging safety check.")
            with DragonFlyController("192.168.0.111", 10000) as controller:
                sensor_0_state = classify_sensor_state(controller.get_sensor_status(0))
                logger.info("Sensor 0 state is %s.", sensor_0_state)
                if sensor_0_state == "closed":
                    logger.info("Sensor 0 is closed. Closing relay 5.")
                    set_relay_5(controller, "closed", logger, args.dry_run)
                else:
                    logger.info("Sensor 0 is open. Leaving relay 5 as-is.")
            sys.exit(0)

        logger.error("Unexpected time-state logic branch.")
        sys.exit(1)

    except Exception:
        logger.exception("Unhandled error.")
        sys.exit(1)