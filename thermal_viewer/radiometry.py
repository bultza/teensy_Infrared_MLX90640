"""EEPROM-based temperature conversion with each subpage's own auxiliary data."""
import math
import struct

from .vendor.radiometry_math import RadiometryMath


class Radiometry:
    def __init__(self, words, emissivity=0.95, reflected_temperature=None):
        if not math.isfinite(emissivity) or not 0 < emissivity <= 1:
            raise ValueError('Emissivity must be finite and in (0, 1]')
        if reflected_temperature is not None and (not math.isfinite(reflected_temperature) or reflected_temperature <= -273.15):
            raise ValueError('Reflected temperature must be finite and above absolute zero')
        if len(words) != 832 or any(type(word) is not int or not 0 <= word <= 65535 for word in words):
            raise ValueError('Calibration requires 832 unsigned 16-bit EEPROM words')
        if len(set(words)) == 1:
            raise ValueError('Uniform EEPROM is not valid calibration')
        try:
            self.math = RadiometryMath(words)
        except (ArithmeticError, ValueError) as exc:
            raise ValueError(f'Invalid EEPROM calibration: {exc}') from exc
        if not self.math.kVdd or not self.math.KtPTAT or not self.math.gainEE or any(a <= 0 for a in self.math.alpha):
            raise ValueError('Calibration contains invalid divisors or pixel sensitivity')
        self.emissivity = emissivity
        self.reflected_temperature = reflected_temperature
        self.last_ambient = None
        self.last_vdd = None
        self.bad_pixels = set(self.math.brokenPixels + self.math.outlierPixels)

    def convert_pair(self, first, second):
        """Convert a validated pair independently, retaining auxiliary data per subpage."""
        result = [math.nan] * 768
        ambient, voltages = [], []
        try:
            for payload in (first, second):
                if len(payload) != 1690:
                    raise ValueError('Invalid radiometric subpage length')
                page = payload[20]
                if page not in (0, 1):
                    raise ValueError('Invalid radiometric page')
                control = struct.unpack_from('<H', payload, 24)[0]
                frame = list(struct.unpack_from('<832H', payload, 26)) + [control, page]
                if not frame[778]:
                    raise ValueError('Invalid zero gain measurement')
                for pixel in range(768):
                    row, column = divmod(pixel, 32)
                    selected = (row ^ column) & 1 if control & 0x1000 else row & 1
                    if selected == page and pixel not in self.bad_pixels and frame[pixel] == 0x7fff:
                        raise ValueError('Invalid pixel data marker')
                # Melexis auxiliary-data validity markers; do not turn invalid RAM into temperatures.
                required_aux = [0] + list(range(8, 19)) + list(range(20, 23)) + list(range(24, 33)) + list(range(40, 51)) + list(range(52, 55)) + list(range(56, 64))
                if any(frame[768 + i] == 0x7fff for i in required_aux):
                    raise ValueError('Invalid auxiliary data marker')
                ta = self.math._GetTa(frame)
                vdd = self.math._GetVdd(frame)
                if not math.isfinite(ta) or not math.isfinite(vdd):
                    raise ValueError('Nonfinite ambient temperature or supply voltage')
                # Adafruit's open-air default, adjustable without writing sensor registers.
                reflected = ta - 8 if self.reflected_temperature is None else self.reflected_temperature
                self.math._CalculateTo(frame, self.emissivity, reflected, result)
                ambient.append(ta)
                voltages.append(vdd)
        except (ArithmeticError, ValueError) as exc:
            raise ValueError(f'Radiometric conversion failed: {exc}') from exc
        # Defective pixels remain masked rather than being presented as measured -273.15 °C.
        for pixel in self.bad_pixels:
            result[pixel] = math.nan
        if any(not math.isfinite(value) for i, value in enumerate(result) if i not in self.bad_pixels):
            raise ValueError('Incomplete or nonfinite temperature image')
        self.last_ambient = sum(ambient) / 2
        self.last_vdd = sum(voltages) / 2
        self.last_subpages = (bytes(first), bytes(second))
        return result
