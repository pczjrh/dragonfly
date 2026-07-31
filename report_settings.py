#!/usr/bin/env python3
import argparse
from dragonfly import DragonFlyController


def parse_args():
    parser = argparse.ArgumentParser(description="Report DragonFly sensor and relay settings")
    parser.add_argument("--ip", default="192.168.0.111", help="DragonFly controller IP address")
    parser.add_argument("--port", type=int, default=10000, help="DragonFly controller UDP port")
    parser.add_argument("--sensor-start", type=int, default=0, help="First sensor ID to query")
    parser.add_argument("--sensor-end", type=int, default=7, help="Last sensor ID to query")
    parser.add_argument("--relay-start", type=int, default=0, help="First relay ID to query")
    parser.add_argument("--relay-end", type=int, default=7, help="Last relay ID to query")
    parser.add_argument("--timeout", type=float, default=2.0, help="Socket timeout in seconds")
    return parser.parse_args()


def classify_sensor_state(status_value):
    try:
        return "open" if int(str(status_value).strip()) > 400 else "closed"
    except ValueError:
        return "unknown"


def report_sensors(controller, start_id, end_id):
    print("Sensors:")
    for sensor_id in range(start_id, end_id + 1):
        try:
            status, raw = controller.get_sensor_data(sensor_id)
            parsed = controller.parse_sensor_info(raw)
            state = classify_sensor_state(status)
            print(f"  sensor {sensor_id}: state={state} | name={parsed['name']}")
        except Exception as exc:  # noqa: BLE001
            print(f"  sensor {sensor_id}: ERROR: {exc}")


def report_relays(controller, start_id, end_id):
    print("Relays:")
    for relay_id in range(start_id, end_id + 1):
        try:
            status, raw = controller.get_relay_data(relay_id)
            parsed = controller.parse_relay_info(raw)
            print(
                f"  relay {relay_id}: status={status} | name={parsed['name']} | "
                f"current_state={parsed['current_state']} | active={parsed['is_active']}"
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  relay {relay_id}: ERROR: {exc}")


def main():
    args = parse_args()

    print(f"Connecting to {args.ip}:{args.port}")
    with DragonFlyController(args.ip, args.port, timeout=args.timeout) as controller:
        report_sensors(controller, args.sensor_start, args.sensor_end)
        print()
        report_relays(controller, args.relay_start, args.relay_end)


if __name__ == "__main__":
    main()
