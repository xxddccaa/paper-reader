"""Render retained original figure assets with production code, without model calls."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.structured_source import render_image, combine_images, RENDER_RULES_VERSION
from PIL import Image, ImageDraw


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() and any(args.output.iterdir()):
        parser.error("Output must be fresh")
    payload = json.loads(args.corpus.read_text())
    assets = {asset["url"]: asset for asset in payload["visual_assets"].values()}
    main = next(doc for doc in payload["documents"] if doc["id"] == "main")
    args.output.mkdir(parents=True, exist_ok=True)
    figures = main["coverage"]["figures"]
    sheet = Image.new("RGB", (1000, 330 * ((len(figures) + 1) // 2)), "white")
    draw = ImageDraw.Draw(sheet)
    rows = []
    for index, figure in enumerate(figures):
        panels = []
        for image in figure["images"]:
            asset = assets[image["url"]]
            original = (args.corpus.parent / asset["path"]).read_bytes()
            assert hashlib.sha256(original).hexdigest() == asset["sha256"]
            panels.append(render_image(original, asset.get("content_type", ""))[0])
        rendered = combine_images(panels)
        name = re.sub(r"[^A-Za-z0-9_-]", "_", figure["id"]) + ".png"
        (args.output / name).write_bytes(rendered)
        raster = Image.open(io.BytesIO(rendered)).convert("RGB")
        rows.append({"figure_id": figure["id"], "file": name, "sha256": hashlib.sha256(rendered).hexdigest(),
                     "width": raster.width, "height": raster.height})
        raster.thumbnail((490, 290))
        x, y = (index % 2) * 500, (index // 2) * 330
        sheet.paste(raster, (x, y + 25))
        draw.text((x + 10, y + 5), figure["id"], fill="black")
    sheet.save(args.output / "contact.png")
    report = {"render_rules": RENDER_RULES_VERSION, "model_calls": 0, "figures": rows}
    (args.output / "render-report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report))


if __name__ == "__main__":
    main()
