"""Generate offline test vectors with an external, unmodified Melexis C reference.

Input JSON contains `eeprom` (832 words) and `pages` (two base64 SUBPAGE payloads).
The reference directory contains MLX90640_API.c, MLX90640_API.h, and
MLX90640_I2C_Driver.h from https://github.com/melexis/mlx90640-library.
The compiler is needed only to regenerate vectors, not to run the viewer/tests.
"""
import argparse
import base64
import ctypes as ct
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import tempfile

WRAPPER = '''
#include <stdint.h>
#include "MLX90640_API.h"
int MLX90640_I2CRead(uint8_t a,uint16_t b,uint16_t c,uint16_t*d){return -1;}
int MLX90640_I2CWrite(uint8_t a,uint16_t b,uint16_t c){return -1;}
int MLX90640_I2CGeneralReset(void){return -1;}
int reference(uint16_t* ee,uint16_t* frame,float emissivity,float tr,float* out,float* ta,float* vdd){
    paramsMLX90640 params;
    int result=MLX90640_ExtractParameters(ee,&params);
    if(result) return result;
    *ta=MLX90640_GetTa(frame,&params);
    *vdd=MLX90640_GetVdd(frame,&params);
    MLX90640_CalculateTo(frame,&params,emissivity,tr,out);
    return 0;
}
'''


def generate(reference, data):
    with tempfile.TemporaryDirectory() as directory:
        directory = Path(directory)
        (directory/'wrapper.c').write_text(WRAPPER)
        subprocess.run(['gcc', '-shared', '-fPIC', '-O2', '-include', 'stdint.h',
                        '-I', str(reference), str(reference/'MLX90640_API.c'),
                        str(directory/'wrapper.c'), '-lm', '-o', str(directory/'reference.so')], check=True)
        lib = ct.CDLL(str(directory/'reference.so'))
        u16, f32 = ct.POINTER(ct.c_uint16), ct.POINTER(ct.c_float)
        lib.reference.argtypes = [u16, u16, ct.c_float, ct.c_float, f32, f32, f32]
        lib.reference.restype = ct.c_int
        ee = (ct.c_uint16*832)(*data['eeprom'])
        cases = []
        for chess in (True, False):
            for emissivity, reflected in ((0.95, None), (0.8, 20.0), (1.0, 35.0)):
                output = (ct.c_float*768)(*([float('nan')]*768))
                temperatures, voltages = [], []
                pages = []
                for encoded in data['pages']:
                    payload = bytearray(base64.b64decode(encoded))
                    control = struct.unpack_from('<H', payload, 24)[0]
                    control = control | 0x1000 if chess else control & ~0x1000
                    struct.pack_into('<H', payload, 24, control)
                    pages.append(base64.b64encode(payload).decode())
                    frame = (ct.c_uint16*834)(*struct.unpack_from('<832H', payload, 26), control, payload[20])
                    ta, vdd = ct.c_float(), ct.c_float()
                    # First call obtains reference ambient for the default reflected-temperature assumption.
                    status = lib.reference(ee, frame, emissivity, 20, output, ct.byref(ta), ct.byref(vdd))
                    if status:
                        raise ValueError(f'Melexis calibration error {status}')
                    tr = ta.value - 8 if reflected is None else reflected
                    lib.reference(ee, frame, emissivity, tr, output, ct.byref(ta), ct.byref(vdd))
                    temperatures.append(ta.value)
                    voltages.append(vdd.value)
                cases.append(dict(chess=chess, emissivity=emissivity, reflected_temperature=reflected,
                                  pages=pages, expected=list(output), ambient=sum(temperatures)/2,
                                  vdd=sum(voltages)/2))
        return dict(source='Melexis MLX90640_ExtractParameters/GetTa/GetVdd/CalculateTo, gcc -O2',
                    source_sha256=hashlib.sha256((reference/'MLX90640_API.c').read_bytes()).hexdigest(),
                    input_provenance=data.get('input_provenance', 'Unspecified inputs; review before publication'),
                    input_generator=data.get('input_generator'),
                    eeprom=data['eeprom'], cases=cases)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference-dir', type=Path, required=True)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(json.dumps(generate(args.reference_dir, json.loads(args.input.read_text())))+'\n')
