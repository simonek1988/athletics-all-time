"""Generate PNG favicon variants from favicon.svg using Playwright."""
import pathlib
from playwright.sync_api import sync_playwright

svg_content = pathlib.Path("static/favicon.svg").read_text(encoding="utf-8")

sizes = {
    "favicon-32x32.png":    32,
    "favicon-192x192.png":  192,
    "apple-touch-icon.png": 180,
}


def html_page(svg_markup, size):
    # Embed SVG inline to avoid file:// cross-origin issues
    return f"""<!DOCTYPE html>
<html><head><style>
  * {{ margin: 0; padding: 0; }}
  body {{ width: {size}px; height: {size}px; overflow: hidden; background: transparent; }}
  svg {{ width: {size}px; height: {size}px; display: block; }}
</style></head>
<body>{svg_markup}</body></html>"""


with sync_playwright() as p:
    browser = p.chromium.launch()
    for fname, size in sizes.items():
        page = browser.new_page(viewport={"width": size, "height": size})
        page.set_content(html_page(svg_content, size))
        page.wait_for_load_state("load")
        out = f"static/{fname}"
        page.screenshot(
            path=out,
            clip={"x": 0, "y": 0, "width": size, "height": size},
            omit_background=True,
        )
        print(f"wrote {out}")
    browser.close()

print("done")
