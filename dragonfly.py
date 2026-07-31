import re
import socket
import time


class DragonFlyError(Exception):
    pass


class DragonFlyConnectionError(DragonFlyError):
    pass


class DragonFlyTimeoutError(DragonFlyError):
    pass


class DragonFlyProtocolError(DragonFlyError):
    pass


class DragonFlyController:
    # Operatives and Models as defined in C++ code
    OPERATIVES = ["", "Bootloader", "Error"]
    MODELS = ["Error", "Seletek", "Armadillo", "Platypus", "Dragonfly"]

    def __init__(self, ip, port, timeout=2.0, retries=3, retry_backoff_seconds=0.2):
        self.ip = ip
        self.port = port
        self.timeout = float(timeout)
        self.retries = int(retries)
        self.retry_backoff_seconds = float(retry_backoff_seconds)
        self.sock = None

    def __enter__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(self.timeout)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    @staticmethod
    def _validate_non_negative_int(name, value):
        if not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer. Got: {value!r}")

    @staticmethod
    def _validate_relay_state(state):
        if state not in {"open", "closed"}:
            raise ValueError(f"Invalid relay state: {state!r}. Expected 'open' or 'closed'.")

    def send_command(self, command):
        if self.sock is None:
            raise RuntimeError("Connection not established. Use 'with DragonFlyController(...) as controller:'")

        last_error = None
        for attempt in range(1, self.retries + 1):
            try:
                self.sock.sendto(command.encode(), (self.ip, self.port))
                data, _ = self.sock.recvfrom(1024)
                return data.decode(errors="replace").strip()
            except socket.timeout as e:
                last_error = e
                if attempt < self.retries:
                    time.sleep(self.retry_backoff_seconds * attempt)
                    continue
                raise DragonFlyTimeoutError(
                    f"Command '{command}' timed out after {self.retries} attempts."
                ) from e
            except OSError as e:
                last_error = e
                if attempt < self.retries:
                    time.sleep(self.retry_backoff_seconds * attempt)
                    continue
                raise DragonFlyConnectionError(
                    f"Socket error on command '{command}' after {self.retries} attempts: {e}"
                ) from e

        raise DragonFlyConnectionError(
            f"Command '{command}' failed after {self.retries} attempts. Last error: {last_error}"
        )

    @staticmethod
    def _parse_relay_status_response(response):
        if not response:
            raise DragonFlyProtocolError("Empty relay status response.")
        # Parse trailing 0/1 before optional '#'
        match = re.search(r"([01])#?\s*$", response)
        if not match:
            raise DragonFlyProtocolError(f"Malformed relay status response: {response!r}")
        return "closed" if match.group(1) == "1" else "open"

    def get_relay_status(self, relay_id):
        self._validate_non_negative_int("relay_id", relay_id)
        command = f"!relio rldgrd 0 {relay_id}#"
        response = self.send_command(command)
        return self._parse_relay_status_response(response)

    def get_sensor_status(self, sensor_id):
        self._validate_non_negative_int("sensor_id", sensor_id)
        command = f"!relio snanrd 0 {sensor_id}#"
        response = self.send_command(command)
        if not response:
            raise DragonFlyProtocolError("Empty sensor status response.")

        # Preferred format: "...:<value>#"
        if ":" in response:
            value = response.split(":", 1)[1].strip().rstrip("#").strip()
            if value:
                return value

        # Fallback: last signed numeric token before '#'
        match = re.search(r"(-?\d+(?:\.\d+)?)#?\s*$", response)
        if match:
            return match.group(1)

        raise DragonFlyProtocolError(f"Malformed sensor status response: {response!r}")

    def get_relay_data(self, relay_id):
        self._validate_non_negative_int("relay_id", relay_id)
        relay_data = self.send_command(f"!relio getreldata {relay_id}#")
        status = self.get_relay_status(relay_id)
        relay = f"{relay_data},{status}"
        return status, relay

    def get_sensor_data(self, sensor_id):
        self._validate_non_negative_int("sensor_id", sensor_id)
        sensor_data = self.send_command(f"!relio getsendata {sensor_id}#")
        status = self.get_sensor_status(sensor_id)
        sensor = f"{sensor_data},{status}"
        return status, sensor

    @staticmethod
    def parse_relay_info(relay_string):
        parts = relay_string.split(",")
        if len(parts) < 7:
            raise DragonFlyProtocolError(
                f"Relay data has too few fields ({len(parts)}). Data: {relay_string!r}"
            )

        is_active = parts[3].isdigit() and parts[3] == "1"
        timeout = int(parts[5]) if parts[5].isdigit() else None

        return {
            "name": parts[0],
            "state_open": parts[1],
            "state_closed": parts[2],
            "is_active": is_active,
            "timeout": timeout,
            "current_state": parts[-1],
        }

    @staticmethod
    def parse_sensor_info(sensor_string):
        parts = sensor_string.split(",")
        if len(parts) < 2:
            raise DragonFlyProtocolError(
                f"Sensor data has too few fields ({len(parts)}). Data: {sensor_string!r}"
            )
        return {"name": parts[0], "current_value": parts[-1]}

    @staticmethod
    def parse_version_response(response):
        # Extract numeric part from the response using regex
        match = re.search(r"(\d+)", response)
        if match:
            return int(match.group(1))
        raise DragonFlyProtocolError("No numeric version info found in response.")

    def change_relay_state(self, relay_id):
        """Toggles the state of the specified relay."""
        self._validate_non_negative_int("relay_id", relay_id)
        command = f"!relio rlchg 2 {relay_id} 1#"
        return self.send_command(command)

    def set_relay_state(self, relay_id, state):
        """Sets relay to open (0) or closed (1)."""
        self._validate_non_negative_int("relay_id", relay_id)
        self._validate_relay_state(state)

        state_val = "1" if state == "closed" else "0"
        command = f"!relio rlset 2 {relay_id} {state_val}#"
        return self.send_command(command)

    def echo(self):
        response = self.send_command("!seletek version#")
        if not response:
            return None

        res = self.parse_version_response(response.strip())

        # Extract version information
        oper = res // 10000  # Operational mode
        model = (res // 1000) % 10  # Model
        fwmaj = (res // 100) % 10  # Firmware major version
        fwmin = res % 100  # Firmware minor version

        if oper >= len(self.OPERATIVES):
            oper = -1
        if model >= len(self.MODELS):
            model = 0

        version_info = f"{self.OPERATIVES[oper]} {self.MODELS[model]} fwv {fwmaj}.{fwmin}"
        return version_info

