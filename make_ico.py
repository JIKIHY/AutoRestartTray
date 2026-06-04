# -*- coding: utf-8 -*-
"""빌드용 exe 아이콘(app.ico) 생성 — build.bat 에서 자동 실행됩니다."""
from PIL import Image, ImageDraw


def make(sz):
    img = Image.new("RGBA", (sz, sz), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = sz / 64.0
    d.ellipse([3 * s, 3 * s, 61 * s, 61 * s], fill=(33, 150, 243, 255))
    w = max(2, int(5 * s))
    d.arc([20 * s, 20 * s, 44 * s, 44 * s], start=300, end=240, fill="white", width=w)
    d.line([32 * s, 14 * s, 32 * s, 34 * s], fill="white", width=w)
    return img


if __name__ == "__main__":
    base = make(256)
    base.save("app.ico", sizes=[(16, 16), (24, 24), (32, 32),
                                (48, 48), (64, 64), (128, 128), (256, 256)])
    print("app.ico created")
