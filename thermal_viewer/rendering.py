"""Shared palette/range rendering for desktop display and exported timelapse frames."""
import numpy as np
from matplotlib import colormaps
from PIL import Image, ImageDraw

PALETTES = ('inferno', 'magma', 'viridis', 'plasma', 'gray')


def colorize(values, palette='inferno', limits=None):
    values = np.asarray(values, dtype=float).reshape(24, 32)
    finite = np.isfinite(values)
    if not finite.any():
        raise ValueError('Image contains no finite measurements')
    low, high = (float(values[finite].min()), float(values[finite].max())) if limits is None else limits
    if not np.isfinite([low, high]).all() or high < low:
        raise ValueError('Display minimum must not exceed maximum')
    high = max(high, low + 0.01)
    normalized = np.clip((np.where(finite, values, low) - low) / (high - low), 0, 1)
    rgb = (colormaps[palette](normalized)[..., :3] * 255).astype(np.uint8)
    rgb[~finite] = 0
    return rgb, (low, high)


def recording_image(values, palette, limits, label):
    rgb, scale = colorize(values, palette, limits)
    image = Image.new('RGB', (640, 512), '#111827')
    image.paste(Image.fromarray(rgb).resize((640, 480), Image.Resampling.NEAREST), (0, 0))
    # A caption preserves the per-frame scale when automatic scaling is selected.
    ImageDraw.Draw(image).text((12, 490), f'{label} | scale {scale[0]:.2f} .. {scale[1]:.2f}', fill='white')
    return image, scale
