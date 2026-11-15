"""Run with python -m thermal_viewer --demo or --port DEVICE."""
import argparse
import sys

# Preserve the worker import for callers that used the initial command-line module.
from .receiver import run_serial


def main():
    parser = argparse.ArgumentParser(description='MLX90640 calibrated thermal viewer')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--port', help='USB serial device, e.g. /dev/ttyACM0 or COM3')
    group.add_argument('--demo', action='store_true', help='Show synthetic images without hardware')
    group.add_argument('--list-ports', action='store_true')
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument('--read-eeprom', action='store_true', help='Read/cache the real 832-word EEPROM and exit')
    parser.add_argument('--output', help='New JSON backup path for --read-eeprom')
    operation.add_argument('--diagnose-i2c', action='store_true', help='Read camera status/control once and exit (requires --port)')
    operation.add_argument('--test-pattern', action='store_true', help='Show the synthetic gradient sent by Teensy (requires --port)')
    parser.add_argument('--emissivity', type=float, default=0.95, help='Object emissivity in (0, 1], default 0.95')
    parser.add_argument('--reflected-temperature', type=float, help='Reflected apparent temperature in °C; default: sensor ambient minus 8°C')
    parser.add_argument('--i2c-khz', type=int, choices=(100, 400), default=400, help='Camera I2C clock in kHz, default 400')
    parser.add_argument('--fps', type=float, choices=(0.25, 0.5, 1, 2), default=2, help='Target complete camera images/s, default 2')
    args = parser.parse_args()
    from .settings import configuration_payload
    try:
        configuration_payload(args.i2c_khz, args.fps)
    except ValueError as exc:
        parser.error(str(exc))
    if args.test_pattern and not args.port:
        parser.error('--test-pattern requires --port')
    import math
    if not math.isfinite(args.emissivity) or not 0 < args.emissivity <= 1:
        parser.error('--emissivity must be finite and in (0, 1]')
    if args.reflected_temperature is not None and (not math.isfinite(args.reflected_temperature) or args.reflected_temperature <= -273.15):
        parser.error('--reflected-temperature must be finite and above -273.15°C')
    if args.output and not args.read_eeprom:
        parser.error('--output requires --read-eeprom')
    if args.diagnose_i2c or args.read_eeprom:
        if not args.port:
            parser.error('I2C operations require --port')
        from .diagnostics import diagnose
        try:
            result = diagnose(args.port, args.read_eeprom, args.output)
        except (OSError, ValueError, RuntimeError) as exc:
            print(f'Diagnostic failed: {exc}', file=sys.stderr)
            result = 1
        raise SystemExit(result)
    if args.list_ports:
        from serial.tools.list_ports import comports
        for port in comports():
            print(f'{port.device}: {port.description}')
        return
    try:
        from .gui import run_gui
    except ImportError as exc:
        parser.error(f'Desktop dependencies are missing ({exc}). Run: python -m pip install -r requirements.txt')
    return run_gui(args)


if __name__ == '__main__':
    main()
