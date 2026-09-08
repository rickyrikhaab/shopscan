"""Generate assets/app.ico -- a storefront in the dashboard's own palette.

Drawn at 1024px and downsampled so the small sizes stay crisp. The 16px
rendering is the one that has to work; check assets/icon_sizes.png after
changing anything here.

    python assets/make_icon.py
"""
from PIL import Image, ImageDraw
import pathlib

S = 1024
BG    = (13, 17, 23, 255)     # dashboard background
GREEN = (61, 220, 132, 255)   # dashboard accent
LIGHT = (230, 237, 243, 255)
DARK  = (22, 27, 34, 255)

img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * .22), fill=BG)

body_l, body_r = int(S * .20), int(S * .80)
body_t, body_b = int(S * .46), int(S * .80)
d.rectangle([body_l, body_t, body_r, body_b], fill=LIGHT)

dw, cx = int(S * .11), S // 2
d.rounded_rectangle([cx - dw, int(S * .58), cx + dw, body_b],
                    radius=int(S * .045), fill=DARK)
for x in (int(S * .255), int(S * .625)):
    d.rounded_rectangle([x, int(S * .55), x + int(S * .12), int(S * .655)],
                        radius=int(S * .018), fill=DARK)

aw_t, aw_b = int(S * .29), int(S * .46)
aw_l, aw_r = int(S * .13), int(S * .87)
top_in = int(S * .055)
d.polygon([(aw_l + top_in, aw_t), (aw_r - top_in, aw_t),
           (aw_r, aw_b), (aw_l, aw_b)], fill=GREEN)
span = (aw_r - aw_l) / 5
for i in range(5):
    x0 = aw_l + i * span
    d.ellipse([x0, aw_b - span * .5, x0 + span, aw_b + span * .5], fill=GREEN)

out = pathlib.Path(__file__).resolve().parent
img.save(out / "icon_preview.png")
img.save(out / "app.ico",
         sizes=[(16,16),(20,20),(24,24),(32,32),(40,40),(48,48),
                (64,64),(128,128),(256,256)])

strip = Image.new("RGBA", (16+32+48+64+16*5, 80), (40, 44, 52, 255))
x = 8
for s in (16, 32, 48, 64):
    r = img.resize((s, s), Image.LANCZOS)
    strip.paste(r, (x, (80 - s) // 2), r)
    x += s + 16
strip.save(out / "icon_sizes.png")
print("wrote app.ico, icon_preview.png, icon_sizes.png")
