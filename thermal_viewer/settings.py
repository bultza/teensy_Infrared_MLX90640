"""Host/device contract for conservative acquisition settings."""
import struct

FPS_CODES = {0.25: 0, 0.5: 1, 1.0: 2, 2.0: 3}


def configuration_payload(i2c_khz=400, fps=2.0):
    if i2c_khz not in (100, 400) or fps not in FPS_CODES:
        raise ValueError('Supported I2C rates: 100/400 kHz; complete FPS: 0.25/0.5/1/2')
    if i2c_khz == 100 and fps > 1:
        raise ValueError('100 kHz requires --fps 1 or lower to leave time for coherent RAM reads')
    return struct.pack('<IB', i2c_khz * 1000, FPS_CODES[fps])


def confirmed_configuration(payload, i2c_khz, fps):
    if len(payload) != 12 or payload[:2] != bytes([6, 0]):
        raise ValueError('Invalid SET_CONFIG response')
    clock, control, revision = struct.unpack_from('<IHI', payload, 2)
    if clock != i2c_khz * 1000 or (control >> 7) & 7 != FPS_CODES[fps] or not revision:
        raise ValueError('Configuration readback does not match requested clock/FPS')
    if not control & 1 or control & 0x000C:
        raise ValueError('Confirmed control does not enable supported alternating subpages')
    return revision, control
