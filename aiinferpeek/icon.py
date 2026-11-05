"""程序图标：蓝色圆角方块，里面三根高低不同的白色竖条。托盘和 exe 共用。"""

from PIL import Image, ImageDraw

ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def make_icon_image(size=64):
    scale = 4  # 先画大图再缩小，边缘更平滑
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=s * 0.22, fill=(42, 120, 214, 255))
    bar_w = s * 0.14
    gap = s * 0.08
    left = (s - 3 * bar_w - 2 * gap) / 2
    bottom = s * 0.76
    for i, frac in enumerate((0.32, 0.52, 0.42)):
        x0 = left + i * (bar_w + gap)
        d.rounded_rectangle((x0, bottom - s * frac, x0 + bar_w, bottom), radius=bar_w * 0.35, fill=(255, 255, 255, 255))
    return img.resize((size, size), Image.LANCZOS)


def save_ico(path):
    make_icon_image(256).save(path, format="ICO", sizes=ICO_SIZES)
