import os
import pymupdf
from PIL import Image

def generate_all_favicons():
    static_dir = os.path.join("app", "static")
    img_dir = os.path.join(static_dir, "img")
    os.makedirs(img_dir, exist_ok=True)

    # 1. Pure Vector SVG matching the Karl Michl logo
    # Features the Karl Michl crimson rounded rectangle (#A82A2C) with pure white lowercase "m"
    svg_content = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64">
  <rect width="64" height="64" rx="14" fill="#A82A2C"/>
  <path d="M 12 50 L 12 25 C 12 17.5 17.5 14 23.5 14 C 28.5 14 31.8 17 33.5 21 C 35.5 17 39 14 44.5 14 C 50.5 14 53 18 53 25 L 53 50 L 45.5 50 L 45.5 26.5 C 45.5 21 44 19.5 39.5 19.5 C 35.5 19.5 33.5 22 33.5 27 L 33.5 50 L 26 50 L 26 26.5 C 26 21 24.5 19.5 20 19.5 C 16 19.5 18 22 18 27 L 18 50 Z" fill="#FFFFFF"/>
</svg>"""

    svg_path = os.path.join(static_dir, "favicon.svg")
    with open(svg_path, "w", encoding="utf-8") as f:
        f.write(svg_content)
    print("Saved pure vector SVG:", svg_path)

    # 2. Render vector SVG to high-res master pixmap (1024x1024) via PyMuPDF for perfect antialiasing
    doc = pymupdf.open(stream=svg_content.encode("utf-8"), filetype="svg")
    # 64px * (72 * 16 / 72) -> 1024px width
    zoom_matrix = pymupdf.Matrix(16, 16)
    pix = doc[0].get_pixmap(matrix=zoom_matrix, alpha=True)
    master_im = Image.frombytes("RGBA", [pix.width, pix.height], pix.samples)
    print(f"Master rendered image: {master_im.size}")

    # 3. 512x512 Master icon
    im512 = master_im.resize((512, 512), Image.Resampling.LANCZOS)
    im512.save(os.path.join(img_dir, "michl_icon.png"), "PNG")
    im512.save(os.path.join(static_dir, "favicon-512x512.png"), "PNG")

    # 4. 180x180 Apple Touch Icon
    im180 = master_im.resize((180, 180), Image.Resampling.LANCZOS)
    im180.save(os.path.join(static_dir, "apple-touch-icon.png"), "PNG")

    # 5. 32x32 Standard desktop tab favicon
    im32 = master_im.resize((32, 32), Image.Resampling.LANCZOS)
    im32.save(os.path.join(static_dir, "favicon-32x32.png"), "PNG")

    # 6. 16x16 Small tab favicon
    im16 = master_im.resize((16, 16), Image.Resampling.LANCZOS)
    im16.save(os.path.join(static_dir, "favicon-16x16.png"), "PNG")

    # 7. Multi-resolution ICO (16, 32, 48, 64, 128, 256)
    ico_sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    im512.save(os.path.join(static_dir, "favicon.ico"), format="ICO", sizes=ico_sizes)

    print("Successfully generated all Michl brand favicons and icons!")

if __name__ == "__main__":
    generate_all_favicons()
