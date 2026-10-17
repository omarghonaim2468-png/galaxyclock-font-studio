from flask import Flask, render_template, request, send_file, jsonify
from werkzeug.utils import secure_filename
from fontTools.ttLib import TTFont
from fontTools.pens.transformPen import TransformPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.pens.recordingPen import RecordingPen
from fontTools.pens.boundsPen import BoundsPen
from pathlib import Path
import tempfile, os, io, zipfile

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 40 * 1024 * 1024
ALLOWED = {".ttf", ".otf"}

def glyph_bounds(glyph_set, glyph_name):
    pen = BoundsPen(glyph_set)
    glyph_set[glyph_name].draw(pen)
    return pen.bounds

def transform_font(input_bytes, height_scale=1.0, width_scale=1.0, slant=0.0, embolden=0.0):
    # Scale and slant are true outline transformations. Embolden is applied as
    # an expanded outline using skia-pathops when available.
    font = TTFont(io.BytesIO(input_bytes))
    if "glyf" not in font:
        raise ValueError("النسخة الأولى بتدعم خطوط TrueType بصيغة TTF فقط للتعديل الحقيقي. خطوط OTF/CFF غير مدعومة حاليًا.")
    if font.isVariable():
        # Staticize at default axis values before modifying outlines.
        try:
            from fontTools.varLib.instancer import instantiateVariableFont
            axes = {axis.axisTag: axis.defaultValue for axis in font["fvar"].axes}
            font = instantiateVariableFont(font, axes, inplace=True)
        except Exception:
            pass
    upm = font["head"].unitsPerEm
    sx, sy = float(width_scale), float(height_scale)
    shear = float(slant)
    glyph_set = font.getGlyphSet()
    glyf = font["glyf"]
    hmtx = font["hmtx"].metrics

    # Draw each glyph into a recording pen, then replay through a transform pen.
    for name in font.getGlyphOrder():
        old_width, lsb = hmtx[name]
        source = glyph_set[name]
        rec = RecordingPen()
        source.draw(rec)
        target = RecordingPen()
        # x' = sx*x + shear*y ; y' = sy*y
        matrix = (sx, 0, shear, sy, 0, 0)
        rec.replay(TransformPen(target, matrix))
        pen = TTGlyphPen(None)
        target.replay(pen)
        try:
            glyf[name] = pen.glyph()
        except Exception:
            continue
        hmtx[name] = (max(1, round(old_width * sx)), round(lsb * sx))

    # Update global vertical metrics to match the new height scale.
    for table_name in ("hhea", "OS/2"):
        if table_name == "hhea" and hasattr(font[table_name], "ascent"):
            font[table_name].ascent = round(font[table_name].ascent * sy)
            font[table_name].descent = round(font[table_name].descent * sy)
        elif table_name == "OS/2":
            for attr in ("sTypoAscender", "sTypoDescender", "usWinAscent", "usWinDescent"):
                if hasattr(font[table_name], attr):
                    setattr(font[table_name], attr, round(getattr(font[table_name], attr) * sy))
    font["head"].yMax = round(font["head"].yMax * sy)
    font["head"].yMin = round(font["head"].yMin * sy)

    if embolden:
        try:
            import pathops
            from fontTools.pens.svgPathPen import SVGPathPen
            from fontTools.pens.svgPathPen import SVGPathPen
            # Build and stroke each glyph as SVG path; pathops unions stroke with outline.
            # Pathops stroke APIs vary by version, so fail clearly instead of silently
            # claiming boldening worked.
            for name in font.getGlyphOrder():
                glyph = font.getGlyphSet()[name]
                svgpen = SVGPathPen(font.getGlyphSet())
                glyph.draw(svgpen)
                path = svgpen.getCommands()
                if not path:
                    continue
                try:
                    shape = pathops.parse_path(path)
                    stroked = pathops.stroke(shape, embolden * 2, join=pathops.LineJoin.ROUND, cap=pathops.LineCap.ROUND)
                    combined = pathops.union([shape, stroked])
                    outpen = TTGlyphPen(None)
                    combined.draw(outpen)
                    font["glyf"][name] = outpen.glyph()
                except Exception as exc:
                    raise ValueError("تعذر تطبيق زيادة السُمك على هذا الخط. جرّب قيمة أقل أو استخدم ملف TTF آخر.") from exc
        except ImportError as exc:
            raise ValueError("زيادة السُمك الحقيقية تحتاج تثبيت مكتبة skia-pathops. نفّذ: pip install skia-pathops") from exc

    font["head"].checkSumAdjustment = 0
    output = io.BytesIO()
    font.save(output)
    return output.getvalue()

@app.get("/")
def index():
    return render_template("index.html")

@app.post("/process")
def process():
    file = request.files.get("font")
    if not file or not file.filename:
        return jsonify(error="اختار ملف خط TTF أولًا."), 400
    ext = Path(secure_filename(file.filename)).suffix.lower()
    if ext not in ALLOWED:
        return jsonify(error="ارفع ملف خط بصيغة TTF أو OTF."), 400
    try:
        height = float(request.form.get("height", "1"))
        width = float(request.form.get("width", "1"))
        slant = float(request.form.get("slant", "0"))
        embolden = float(request.form.get("embolden", "0"))
        if not (0.5 <= height <= 2.0 and 0.5 <= width <= 2.0 and -20 <= slant <= 20 and 0 <= embolden <= 120):
            raise ValueError("فيه قيمة خارج النطاق المسموح.")
        data = file.read()
        result = transform_font(data, height, width, slant, embolden)
        out_name = Path(secure_filename(file.filename)).stem + "_modified.ttf"
        return send_file(io.BytesIO(result), as_attachment=True, download_name=out_name, mimetype="font/ttf")
    except Exception as exc:
        return jsonify(error=str(exc)), 400

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
