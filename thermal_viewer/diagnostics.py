"""Explicit one-shot I2C register diagnostic; no sensor configuration commands."""
import os
import struct
import time

from .protocol import Decoder, encode

RECORD = struct.Struct('<HHBBBI')


def parse_diagnostic(payload):
    """Validate the diagnostic response, including structured I2C failures."""
    if len(payload) != 27 or payload[0] != 7 or payload[1] not in (0, 4):
        raise ValueError('Invalid I2C diagnostic response')
    if payload[2] != 0x33 or payload[3] not in (0, 1) or payload[4] not in (0, 1):
        raise ValueError('Invalid diagnostic address or line levels')
    records = [RECORD.unpack_from(payload, offset) for offset in (5, 16)]
    if [record[0] for record in records] != [0x8000, 0x800D]:
        raise ValueError('Unexpected diagnostic registers')
    for reg, value, phase, detail, count, elapsed in records:
        if phase not in range(5) or (phase == 0 and (detail or count != 2)):
            raise ValueError('Invalid diagnostic record')
    if (payload[1] == 0) != all(record[2] == 0 for record in records):
        raise ValueError('Inconsistent diagnostic result')
    return payload[2:5], records


def describe_record(record, expected=2):
    reg, value, phase, detail, count, elapsed = record
    prefix = f'0x{reg:04X}'
    if phase == 0:
        return f'{prefix} = 0x{value:04X} ({elapsed} us)'
    if phase == 1:
        reason = 'bus not idle (SDA/SCL low or controller busy)'
    elif phase == 2:
        reason = {2: 'address NACK', 3: 'register pointer NACK',
                  4: 'Wire timeout or bus/arbitration error'}.get(detail, f'Wire error {detail}')
    elif phase == 3:
        reason = f'incomplete read ({count}/{expected} bytes); read NACK/timeout/bus error is not distinguished by Wire'
    elif phase == 5:
        reason = 'whole EEPROM dump exceeded one-second budget'
    else:
        reason = 'skipped: earlier I2C fault is latched until firmware restart'
    return f'{prefix}: ERROR: {reason} ({elapsed} us)'


def diagnose(port, read_eeprom=False, output=None):
    """Stop synthetic streaming, then perform exactly one diagnostic request."""
    import serial
    if output:
        from pathlib import Path
        if Path(output).exists():
            raise ValueError(f'Output already exists: {output}; choose a new backup filename')
    decoder = Decoder()
    ident = 0
    options = {'exclusive': True} if os.name == 'posix' else {}
    with serial.Serial(port, 115200, timeout=0.05, write_timeout=1, **options) as link:
        time.sleep(0.2)
        link.reset_input_buffer()
        link.write(b'\0')

        def request(command):
            nonlocal ident
            ident += 1
            wire = encode(command, ident)
            if link.write(wire) != len(wire):
                raise RuntimeError('Incomplete USB write')
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                for packet in decoder.feed(link.read(4096)):
                    if packet.kind == 0x80 and packet.identifier == ident:
                        if len(packet.payload) < 2 or packet.payload[0] != command:
                            raise ValueError('Invalid command response')
                        return packet.payload
            raise RuntimeError(f'Command 0x{command:02X} timeout on {port}; diagnostic is not automatically retried')

        hello = None
        for attempt in range(3):
            try:
                hello = request(1)
                break
            except RuntimeError:
                if attempt == 2:
                    raise
        if len(hello) != 13 or hello[1] != 0:
            raise RuntimeError('Invalid HELLO response')
        if not struct.unpack_from('<I', hello, 8)[0] & (4 if read_eeprom else 2):
            raise RuntimeError('Firmware does not support the requested I2C operation; upload the current sketch')
        stopped = request(5)
        if stopped[1]:
            raise RuntimeError(f'STOP failed: {stopped[1]}')
        if read_eeprom:
            # Preserve the legacy five-second startup margin without polling the sensor.
            time.sleep(5)
            payload = request(8)
            if len(payload) < 2:
                raise ValueError('Invalid EEPROM response')
            if payload[1]:
                if payload[1] == 4 and len(payload) == 13:
                    print(describe_record(RECORD.unpack_from(payload, 2), expected=32))
                else:
                    print(f'EEPROM request rejected: result {payload[1]}')
                print('No automatic retry. Check wiring/power; an I2C fault remains latched until firmware restart.')
                return 1
            from .eeprom import export_eeprom
            export_eeprom(payload, output)
            return 0
        payload = request(7)
        if len(payload) == 2:
            raise RuntimeError(f'Diagnostic rejected: result {payload[1]}')
        levels, records = parse_diagnostic(payload)
        print(f'MLX90640 address 0x{levels[0]:02X}, SDA(18)={levels[1]}, SCL(19)={levels[2]} (before reads)')
        for record in records:
            print(describe_record(record))
        if payload[1]:
            print('No automatic retry. Check wiring/power/pull-ups. A locked camera can require a power cycle.')
            return 1
        status = records[0][1]
        print(f'Status: new_data={(status >> 3) & 1}, subpage={status & 1}; status is not cleared.')
        print('Read-only register test complete. This does not validate image acquisition.')
        return 0
