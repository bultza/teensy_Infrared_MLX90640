# SPDX-FileCopyrightText: 2019 ladyada for Adafruit Industries
# SPDX-License-Identifier: MIT
"""Host-only radiometry adapted from Adafruit; no I2C or hardware dependencies.

Source: https://github.com/adafruit/Adafruit_CircuitPython_MLX90640
Original SHA256: 066b153a59489d9a1ecd8ae95aea7ba14b468b5350de593326fe04a77c77e379
Changes: per-instance EEPROM/arrays, bounded zero-coefficient scale loops.
See LICENSE_ADAFRUIT.txt for the original license.
"""
import math
from typing import List, Tuple

SCALEALPHA = 0.000001

class RadiometryMath:
    def __init__(self, words):
        self.eeData = list(words)
        self.kVdd = 0
        self.vdd25 = 0
        self.KvPTAT = 0
        self.KtPTAT = 0
        self.vPTAT25 = 0
        self.alphaPTAT = 0
        self.gainEE = 0
        self.tgc = 0
        self.KsTa = 0
        self.resolutionEE = 0
        self.calibrationModeEE = 0
        self.ksTo = [0] * 5
        self.ct = [0] * 5
        self.alpha = [0] * 768
        self.alphaScale = 0
        self.offset = [0] * 768
        self.kta = [0] * 768
        self.ktaScale = 0
        self.kv = [0] * 768
        self.kvScale = 0
        self.cpAlpha = [0] * 2
        self.cpOffset = [0] * 2
        self.ilChessC = [0] * 3
        self.brokenPixels = []
        self.outlierPixels = []
        self.cpKta = 0
        self.cpKv = 0
        self._ExtractParameters()

    def _GetTa(self, frameData: List[int]) -> float:
        """Compute GetTa using factory calibration parameters."""
        vdd = self._GetVdd(frameData)
        ptat = frameData[800]
        if ptat > 32767:
            ptat -= 65536
        ptatArt = frameData[768]
        if ptatArt > 32767:
            ptatArt -= 65536
        ptatArt = ptat / (ptat * self.alphaPTAT + ptatArt) * math.pow(2, 18)
        ta = ptatArt / (1 + self.KvPTAT * (vdd - 3.3)) - self.vPTAT25
        ta = ta / self.KtPTAT + 25
        return ta

    def _GetVdd(self, frameData: List[int]) -> int:
        """Compute GetVdd using factory calibration parameters."""
        vdd = frameData[810]
        if vdd > 32767:
            vdd -= 65536
        resolutionRAM = (frameData[832] & 3072) >> 10
        resolutionCorrection = math.pow(2, self.resolutionEE) / math.pow(2, resolutionRAM)
        vdd = (resolutionCorrection * vdd - self.vdd25) / self.kVdd + 3.3
        return vdd

    def _CalculateTo(self, frameData: List[int], emissivity: float, tr: float, result: List[float]) -> None:
        """Compute CalculateTo using factory calibration parameters."""
        subPage = frameData[833]
        alphaCorrR = [0] * 4
        irDataCP = [0, 0]
        vdd = self._GetVdd(frameData)
        ta = self._GetTa(frameData)
        ta4 = ta + 273.15
        ta4 *= ta4
        ta4 *= ta4
        tr4 = tr + 273.15
        tr4 *= tr4
        tr4 *= tr4
        taTr = tr4 - (tr4 - ta4) / emissivity
        ktaScale = math.pow(2, self.ktaScale)
        kvScale = math.pow(2, self.kvScale)
        alphaScale = math.pow(2, self.alphaScale)
        alphaCorrR[0] = 1 / (1 + self.ksTo[0] * 40)
        alphaCorrR[1] = 1
        alphaCorrR[2] = 1 + self.ksTo[1] * self.ct[2]
        alphaCorrR[3] = alphaCorrR[2] * (1 + self.ksTo[2] * (self.ct[3] - self.ct[2]))
        gain = frameData[778]
        if gain > 32767:
            gain -= 65536
        gain = self.gainEE / gain
        mode = (frameData[832] & 4096) >> 5
        irDataCP[0] = frameData[776]
        irDataCP[1] = frameData[808]
        for i in range(2):
            if irDataCP[i] > 32767:
                irDataCP[i] -= 65536
            irDataCP[i] *= gain
        irDataCP[0] -= self.cpOffset[0] * (1 + self.cpKta * (ta - 25)) * (1 + self.cpKv * (vdd - 3.3))
        if mode == self.calibrationModeEE:
            irDataCP[1] -= self.cpOffset[1] * (1 + self.cpKta * (ta - 25)) * (1 + self.cpKv * (vdd - 3.3))
        else:
            irDataCP[1] -= (self.cpOffset[1] + self.ilChessC[0]) * (1 + self.cpKta * (ta - 25)) * (1 + self.cpKv * (vdd - 3.3))
        for pixelNumber in range(768):
            if self._IsPixelBad(pixelNumber):
                result[pixelNumber] = -273.15
                continue
            ilPattern = pixelNumber // 32 - pixelNumber // 64 * 2
            chessPattern = ilPattern ^ pixelNumber - pixelNumber // 2 * 2
            conversionPattern = ((pixelNumber + 2) // 4 - (pixelNumber + 3) // 4 + (pixelNumber + 1) // 4 - pixelNumber // 4) * (1 - 2 * ilPattern)
            if mode == 0:
                pattern = ilPattern
            else:
                pattern = chessPattern
            if pattern == frameData[833]:
                irData = frameData[pixelNumber]
                if irData > 32767:
                    irData -= 65536
                irData *= gain
                kta = self.kta[pixelNumber] / ktaScale
                kv = self.kv[pixelNumber] / kvScale
                irData -= self.offset[pixelNumber] * (1 + kta * (ta - 25)) * (1 + kv * (vdd - 3.3))
                if mode != self.calibrationModeEE:
                    irData += self.ilChessC[2] * (2 * ilPattern - 1) - self.ilChessC[1] * conversionPattern
                irData -= self.tgc * irDataCP[subPage]
                irData /= emissivity
                alphaCompensated = SCALEALPHA * alphaScale / self.alpha[pixelNumber]
                alphaCompensated *= 1 + self.KsTa * (ta - 25)
                Sx = alphaCompensated * alphaCompensated * alphaCompensated * (irData + alphaCompensated * taTr)
                Sx = math.sqrt(math.sqrt(Sx)) * self.ksTo[1]
                To = math.sqrt(math.sqrt(irData / (alphaCompensated * (1 - self.ksTo[1] * 273.15) + Sx) + taTr)) - 273.15
                if To < self.ct[1]:
                    torange = 0
                elif To < self.ct[2]:
                    torange = 1
                elif To < self.ct[3]:
                    torange = 2
                else:
                    torange = 3
                To = math.sqrt(math.sqrt(irData / (alphaCompensated * alphaCorrR[torange] * (1 + self.ksTo[torange] * (To - self.ct[torange]))) + taTr)) - 273.15
                result[pixelNumber] = To

    def _ExtractParameters(self) -> None:
        """Compute ExtractParameters using factory calibration parameters."""
        self._ExtractVDDParameters()
        self._ExtractPTATParameters()
        self._ExtractGainParameters()
        self._ExtractTgcParameters()
        self._ExtractResolutionParameters()
        self._ExtractKsTaParameters()
        self._ExtractKsToParameters()
        self._ExtractCPParameters()
        self._ExtractAlphaParameters()
        self._ExtractOffsetParameters()
        self._ExtractKtaPixelParameters()
        self._ExtractKvPixelParameters()
        self._ExtractCILCParameters()
        self._ExtractDeviatingPixels()

    def _ExtractVDDParameters(self) -> None:
        """Compute ExtractVDDParameters using factory calibration parameters."""
        self.kVdd = (self.eeData[51] & 65280) >> 8
        if self.kVdd > 127:
            self.kVdd -= 256
        self.kVdd *= 32
        self.vdd25 = self.eeData[51] & 255
        self.vdd25 = (self.vdd25 - 256 << 5) - 8192

    def _ExtractPTATParameters(self) -> None:
        """Compute ExtractPTATParameters using factory calibration parameters."""
        self.KvPTAT = (self.eeData[50] & 64512) >> 10
        if self.KvPTAT > 31:
            self.KvPTAT -= 64
        self.KvPTAT /= 4096
        self.KtPTAT = self.eeData[50] & 1023
        if self.KtPTAT > 511:
            self.KtPTAT -= 1024
        self.KtPTAT /= 8
        self.vPTAT25 = self.eeData[49]
        self.alphaPTAT = (self.eeData[16] & 61440) / math.pow(2, 14) + 8

    def _ExtractGainParameters(self) -> None:
        """Compute ExtractGainParameters using factory calibration parameters."""
        self.gainEE = self.eeData[48]
        if self.gainEE > 32767:
            self.gainEE -= 65536

    def _ExtractTgcParameters(self) -> None:
        """Compute ExtractTgcParameters using factory calibration parameters."""
        self.tgc = self.eeData[60] & 255
        if self.tgc > 127:
            self.tgc -= 256
        self.tgc /= 32

    def _ExtractResolutionParameters(self) -> None:
        """Compute ExtractResolutionParameters using factory calibration parameters."""
        self.resolutionEE = (self.eeData[56] & 12288) >> 12

    def _ExtractKsTaParameters(self) -> None:
        """Compute ExtractKsTaParameters using factory calibration parameters."""
        self.KsTa = (self.eeData[60] & 65280) >> 8
        if self.KsTa > 127:
            self.KsTa -= 256
        self.KsTa /= 8192

    def _ExtractKsToParameters(self) -> None:
        """Compute ExtractKsToParameters using factory calibration parameters."""
        step = ((self.eeData[63] & 12288) >> 12) * 10
        self.ct[0] = -40
        self.ct[1] = 0
        self.ct[2] = (self.eeData[63] & 240) >> 4
        self.ct[3] = (self.eeData[63] & 3840) >> 8
        self.ct[2] *= step
        self.ct[3] = self.ct[2] + self.ct[3] * step
        KsToScale = (self.eeData[63] & 15) + 8
        KsToScale = 1 << KsToScale
        self.ksTo[0] = self.eeData[61] & 255
        self.ksTo[1] = (self.eeData[61] & 65280) >> 8
        self.ksTo[2] = self.eeData[62] & 255
        self.ksTo[3] = (self.eeData[62] & 65280) >> 8
        for i in range(4):
            if self.ksTo[i] > 127:
                self.ksTo[i] -= 256
            self.ksTo[i] /= KsToScale
        self.ksTo[4] = -0.0002

    def _ExtractCPParameters(self) -> None:
        """Compute ExtractCPParameters using factory calibration parameters."""
        offsetSP = [0] * 2
        alphaSP = [0] * 2
        alphaScale = ((self.eeData[32] & 61440) >> 12) + 27
        offsetSP[0] = self.eeData[58] & 1023
        if offsetSP[0] > 511:
            offsetSP[0] -= 1024
        offsetSP[1] = (self.eeData[58] & 64512) >> 10
        if offsetSP[1] > 31:
            offsetSP[1] -= 64
        offsetSP[1] += offsetSP[0]
        alphaSP[0] = self.eeData[57] & 1023
        if alphaSP[0] > 511:
            alphaSP[0] -= 1024
        alphaSP[0] /= math.pow(2, alphaScale)
        alphaSP[1] = (self.eeData[57] & 64512) >> 10
        if alphaSP[1] > 31:
            alphaSP[1] -= 64
        alphaSP[1] = (1 + alphaSP[1] / 128) * alphaSP[0]
        cpKta = self.eeData[59] & 255
        if cpKta > 127:
            cpKta -= 256
        ktaScale1 = ((self.eeData[56] & 240) >> 4) + 8
        self.cpKta = cpKta / math.pow(2, ktaScale1)
        cpKv = (self.eeData[59] & 65280) >> 8
        if cpKv > 127:
            cpKv -= 256
        kvScale = (self.eeData[56] & 3840) >> 8
        self.cpKv = cpKv / math.pow(2, kvScale)
        self.cpAlpha[0] = alphaSP[0]
        self.cpAlpha[1] = alphaSP[1]
        self.cpOffset[0] = offsetSP[0]
        self.cpOffset[1] = offsetSP[1]

    def _ExtractAlphaParameters(self) -> None:
        """Compute ExtractAlphaParameters using factory calibration parameters."""
        accRemScale = self.eeData[32] & 15
        accColumnScale = (self.eeData[32] & 240) >> 4
        accRowScale = (self.eeData[32] & 3840) >> 8
        alphaScale = ((self.eeData[32] & 61440) >> 12) + 30
        alphaRef = self.eeData[33]
        accRow = [0] * 24
        accColumn = [0] * 32
        alphaTemp = [0] * 768
        for i in range(6):
            p = i * 4
            accRow[p + 0] = self.eeData[34 + i] & 15
            accRow[p + 1] = (self.eeData[34 + i] & 240) >> 4
            accRow[p + 2] = (self.eeData[34 + i] & 3840) >> 8
            accRow[p + 3] = (self.eeData[34 + i] & 61440) >> 12
        for i in range(24):
            if accRow[i] > 7:
                accRow[i] -= 16
        for i in range(8):
            p = i * 4
            accColumn[p + 0] = self.eeData[40 + i] & 15
            accColumn[p + 1] = (self.eeData[40 + i] & 240) >> 4
            accColumn[p + 2] = (self.eeData[40 + i] & 3840) >> 8
            accColumn[p + 3] = (self.eeData[40 + i] & 61440) >> 12
        for i in range(32):
            if accColumn[i] > 7:
                accColumn[i] -= 16
        for i in range(24):
            for j in range(32):
                p = 32 * i + j
                alphaTemp[p] = (self.eeData[64 + p] & 1008) >> 4
                if alphaTemp[p] > 31:
                    alphaTemp[p] -= 64
                alphaTemp[p] *= 1 << accRemScale
                alphaTemp[p] += alphaRef + (accRow[i] << accRowScale) + (accColumn[j] << accColumnScale)
                alphaTemp[p] /= math.pow(2, alphaScale)
                alphaTemp[p] -= self.tgc * (self.cpAlpha[0] + self.cpAlpha[1]) / 2
                alphaTemp[p] = SCALEALPHA / alphaTemp[p]
        temp = max(alphaTemp)
        alphaScale = 0
        while 0 < temp < 32768:
            temp *= 2
            alphaScale += 1
        for i in range(768):
            temp = alphaTemp[i] * math.pow(2, alphaScale)
            self.alpha[i] = int(temp + 0.5)
        self.alphaScale = alphaScale

    def _ExtractOffsetParameters(self) -> None:
        """Compute ExtractOffsetParameters using factory calibration parameters."""
        occRow = [0] * 24
        occColumn = [0] * 32
        occRemScale = self.eeData[16] & 15
        occColumnScale = (self.eeData[16] & 240) >> 4
        occRowScale = (self.eeData[16] & 3840) >> 8
        offsetRef = self.eeData[17]
        if offsetRef > 32767:
            offsetRef -= 65536
        for i in range(6):
            p = i * 4
            occRow[p + 0] = self.eeData[18 + i] & 15
            occRow[p + 1] = (self.eeData[18 + i] & 240) >> 4
            occRow[p + 2] = (self.eeData[18 + i] & 3840) >> 8
            occRow[p + 3] = (self.eeData[18 + i] & 61440) >> 12
        for i in range(24):
            if occRow[i] > 7:
                occRow[i] -= 16
        for i in range(8):
            p = i * 4
            occColumn[p + 0] = self.eeData[24 + i] & 15
            occColumn[p + 1] = (self.eeData[24 + i] & 240) >> 4
            occColumn[p + 2] = (self.eeData[24 + i] & 3840) >> 8
            occColumn[p + 3] = (self.eeData[24 + i] & 61440) >> 12
        for i in range(32):
            if occColumn[i] > 7:
                occColumn[i] -= 16
        for i in range(24):
            for j in range(32):
                p = 32 * i + j
                self.offset[p] = (self.eeData[64 + p] & 64512) >> 10
                if self.offset[p] > 31:
                    self.offset[p] -= 64
                self.offset[p] *= 1 << occRemScale
                self.offset[p] += offsetRef + (occRow[i] << occRowScale) + (occColumn[j] << occColumnScale)

    def _ExtractKtaPixelParameters(self) -> None:
        """Compute ExtractKtaPixelParameters using factory calibration parameters."""
        KtaRC = [0] * 4
        ktaTemp = [0] * 768
        KtaRoCo = (self.eeData[54] & 65280) >> 8
        if KtaRoCo > 127:
            KtaRoCo -= 256
        KtaRC[0] = KtaRoCo
        KtaReCo = self.eeData[54] & 255
        if KtaReCo > 127:
            KtaReCo -= 256
        KtaRC[2] = KtaReCo
        KtaRoCe = (self.eeData[55] & 65280) >> 8
        if KtaRoCe > 127:
            KtaRoCe -= 256
        KtaRC[1] = KtaRoCe
        KtaReCe = self.eeData[55] & 255
        if KtaReCe > 127:
            KtaReCe -= 256
        KtaRC[3] = KtaReCe
        ktaScale1 = ((self.eeData[56] & 240) >> 4) + 8
        ktaScale2 = self.eeData[56] & 15
        for i in range(24):
            for j in range(32):
                p = 32 * i + j
                split = 2 * (p // 32 - p // 64 * 2) + p % 2
                ktaTemp[p] = (self.eeData[64 + p] & 14) >> 1
                if ktaTemp[p] > 3:
                    ktaTemp[p] -= 8
                ktaTemp[p] *= 1 << ktaScale2
                ktaTemp[p] += KtaRC[split]
                ktaTemp[p] /= math.pow(2, ktaScale1)
        temp = abs(ktaTemp[0])
        for kta in ktaTemp:
            temp = max(temp, abs(kta))
        ktaScale1 = 0
        while 0 < temp < 64:
            temp *= 2
            ktaScale1 += 1
        for i in range(768):
            temp = ktaTemp[i] * math.pow(2, ktaScale1)
            if temp < 0:
                self.kta[i] = int(temp - 0.5)
            else:
                self.kta[i] = int(temp + 0.5)
        self.ktaScale = ktaScale1

    def _ExtractKvPixelParameters(self) -> None:
        """Compute ExtractKvPixelParameters using factory calibration parameters."""
        KvT = [0] * 4
        kvTemp = [0] * 768
        KvRoCo = (self.eeData[52] & 61440) >> 12
        if KvRoCo > 7:
            KvRoCo -= 16
        KvT[0] = KvRoCo
        KvReCo = (self.eeData[52] & 3840) >> 8
        if KvReCo > 7:
            KvReCo -= 16
        KvT[2] = KvReCo
        KvRoCe = (self.eeData[52] & 240) >> 4
        if KvRoCe > 7:
            KvRoCe -= 16
        KvT[1] = KvRoCe
        KvReCe = self.eeData[52] & 15
        if KvReCe > 7:
            KvReCe -= 16
        KvT[3] = KvReCe
        kvScale = (self.eeData[56] & 3840) >> 8
        for i in range(24):
            for j in range(32):
                p = 32 * i + j
                split = 2 * (p // 32 - p // 64 * 2) + p % 2
                kvTemp[p] = KvT[split]
                kvTemp[p] /= math.pow(2, kvScale)
        temp = abs(kvTemp[0])
        for kv in kvTemp:
            temp = max(temp, abs(kv))
        kvScale = 0
        while 0 < temp < 64:
            temp *= 2
            kvScale += 1
        for i in range(768):
            temp = kvTemp[i] * math.pow(2, kvScale)
            if temp < 0:
                self.kv[i] = int(temp - 0.5)
            else:
                self.kv[i] = int(temp + 0.5)
        self.kvScale = kvScale

    def _ExtractCILCParameters(self) -> None:
        """Compute ExtractCILCParameters using factory calibration parameters."""
        ilChessC = [0] * 3
        self.calibrationModeEE = (self.eeData[10] & 2048) >> 4
        self.calibrationModeEE ^= 128
        ilChessC[0] = self.eeData[53] & 63
        if ilChessC[0] > 31:
            ilChessC[0] -= 64
        ilChessC[0] /= 16.0
        ilChessC[1] = (self.eeData[53] & 1984) >> 6
        if ilChessC[1] > 15:
            ilChessC[1] -= 32
        ilChessC[1] /= 2.0
        ilChessC[2] = (self.eeData[53] & 63488) >> 11
        if ilChessC[2] > 15:
            ilChessC[2] -= 32
        ilChessC[2] /= 8.0
        self.ilChessC = ilChessC

    def _ExtractDeviatingPixels(self) -> None:
        """Compute ExtractDeviatingPixels using factory calibration parameters."""
        pixCnt = 0
        while pixCnt < 768 and len(self.brokenPixels) < 5 and (len(self.outlierPixels) < 5):
            if self.eeData[pixCnt + 64] == 0:
                self.brokenPixels.append(pixCnt)
            elif self.eeData[pixCnt + 64] & 1 != 0:
                self.outlierPixels.append(pixCnt)
            pixCnt += 1
        if len(self.brokenPixels) > 4:
            raise RuntimeError('More than 4 broken pixels')
        if len(self.outlierPixels) > 4:
            raise RuntimeError('More than 4 outlier pixels')
        if len(self.brokenPixels) + len(self.outlierPixels) > 4:
            raise RuntimeError('More than 4 faulty pixels')
        for brokenPixel1, brokenPixel2 in self._UniqueListPairs(self.brokenPixels):
            if self._ArePixelsAdjacent(brokenPixel1, brokenPixel2):
                raise RuntimeError('Adjacent broken pixels')
        for outlierPixel1, outlierPixel2 in self._UniqueListPairs(self.outlierPixels):
            if self._ArePixelsAdjacent(outlierPixel1, outlierPixel2):
                raise RuntimeError('Adjacent outlier pixels')
        for brokenPixel in self.brokenPixels:
            for outlierPixel in self.outlierPixels:
                if self._ArePixelsAdjacent(brokenPixel, outlierPixel):
                    raise RuntimeError('Adjacent broken and outlier pixels')

    def _UniqueListPairs(self, inputList: List[int]) -> Tuple[int, int]:
        """Compute UniqueListPairs using factory calibration parameters."""
        for i, listValue1 in enumerate(inputList):
            for listValue2 in inputList[i + 1:]:
                yield (listValue1, listValue2)

    def _ArePixelsAdjacent(self, pix1: int, pix2: int) -> bool:
        """Compute ArePixelsAdjacent using factory calibration parameters."""
        pixPosDif = pix1 - pix2
        if -34 < pixPosDif < -30:
            return True
        if -2 < pixPosDif < 2:
            return True
        if 30 < pixPosDif < 34:
            return True
        return False

    def _IsPixelBad(self, pixel: int) -> bool:
        """Compute IsPixelBad using factory calibration parameters."""
        if pixel in self.brokenPixels or pixel in self.outlierPixels:
            return True
        return False
