"""Лабораторная работа 1. Рамка для изображения, вариант 16."""
import base64
import hmac
import os
import re
import secrets
import time
import warnings
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock

from flask import Flask, render_template, request, session, send_file, url_for
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError
from matplotlib.figure import Figure

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
    MAX_CONTENT_LENGTH=5 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE") == "1",
)
MAX_PIXELS = 4_000_000
MAX_RESULT_PIXELS = 8_000_000
plot_lock = Lock()
captcha_lock = Lock()
used_captchas = {}
result_folder = TemporaryDirectory(prefix="frame-lab-")
file_lock = Lock()


def save_result(image):
    """Храним готовые PNG десять минут, чтобы их можно было скачать."""
    folder = Path(result_folder.name)
    with file_lock:
        files = sorted(folder.glob("*.png"), key=lambda p: p.stat().st_mtime)
        for number, path in enumerate(files):
            if time.time() - path.stat().st_mtime > 600 or number < len(files) - 19:
                path.unlink(missing_ok=True)
        old_name = session.get("download_id", "")
        if re.fullmatch(r"[0-9a-f]{32}", old_name):
            (folder / (old_name + ".png")).unlink(missing_ok=True)
        name = secrets.token_hex(16)
        image.save(folder / (name + ".png"), format="PNG")
        session["download_id"] = name
    return url_for("download", name=name)


@app.get("/download/<name>")
def download(name):
    if not re.fullmatch(r"[0-9a-f]{32}", name) or name != session.get("download_id"):
        return "Файл не найден.", 404
    path = Path(result_folder.name) / (name + ".png")
    if not path.is_file() or time.time() - path.stat().st_mtime > 600:
        return "Срок хранения результата истек. Обработайте изображение еще раз.", 404
    return send_file(path, mimetype="image/png", as_attachment=True, download_name="image-with-frame.png")


def png_url(image):
    """Возвращает PNG прямо в HTML. Загруженные файлы на диск не пишутся."""
    stream = BytesIO()
    image.save(stream, format="PNG")
    return "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode("ascii")


def answer_hash(nonce, answer):
    key = app.secret_key.encode("utf-8")
    return hmac.new(key, (nonce + ":" + answer).encode(), "sha256").hexdigest()


def make_captcha():
    """Новая проверка действует пять минут и только на одну попытку."""
    a, b = secrets.randbelow(8) + 2, secrets.randbelow(8) + 2
    nonce = secrets.token_hex(12)
    session["captcha"] = {
        "nonce": nonce,
        "hash": answer_hash(nonce, str(a + b)),
        "created": time.time(),
    }
    image = Image.new("RGB", (220, 64), "#f3f5f8")
    draw = ImageDraw.Draw(image)
    for _ in range(8):
        xy = [secrets.randbelow(220), secrets.randbelow(64),
              secrets.randbelow(220), secrets.randbelow(64)]
        draw.line(xy, fill="#c4cdd7", width=1)
    draw.text((21, 14), f"{a} + {b} = ?", font=ImageFont.load_default(size=32), fill="#172b45")
    return png_url(image)


def check_captcha(answer):
    data = session.pop("captcha", None)
    if not data or not 0 <= time.time() - data["created"] <= 300:
        return False
    # Сервер помнит использованные проверки, даже если браузер повторил старый cookie.
    with captcha_lock:
        now = time.time()
        for nonce, used_at in list(used_captchas.items()):
            if now - used_at > 300:
                del used_captchas[nonce]
        if data["nonce"] in used_captchas or len(used_captchas) >= 2048:
            return False
        used_captchas[data["nonce"]] = now
    if not re.fullmatch(r"[0-9]{1,2}", answer):
        return False
    return hmac.compare_digest(data["hash"], answer_hash(data["nonce"], answer))


def read_image(stream):
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            with Image.open(stream) as source:
                if source.format not in ("PNG", "JPEG"):
                    raise ValueError("Поддерживаются только изображения PNG и JPG.")
                if source.width * source.height > MAX_PIXELS:
                    raise ValueError("Изображение слишком большое. Допустимо до 4 миллионов пикселей.")
                source.load()
                # Учитываем поворот снимка с телефона, прозрачность показываем на белом фоне.
                source = ImageOps.exif_transpose(source).convert("RGBA")
                result = Image.new("RGB", source.size, "white")
                result.paste(source, mask=source.getchannel("A"))
                return result
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError,
                Image.DecompressionBombWarning) as error:
            raise ValueError("Не удалось открыть изображение. Выберите исправный PNG или JPG.") from error


def add_frame(image, border, color):
    if not 0 <= border <= 300:
        raise ValueError("Ширина рамки должна быть целым числом от 0 до 300 пикселей.")
    width, height = image.size
    if (width + 2 * border) * (height + 2 * border) > MAX_RESULT_PIXELS:
        raise ValueError("Результат слишком большой. Уменьшите изображение или ширину рамки.")
    return ImageOps.expand(image, border=border, fill=color)


def color_histogram(image):
    """По 256 значений для красного, зеленого и синего каналов."""
    values = image.histogram()
    return [values[start:start + 256] for start in (0, 256, 512)]


def histogram_url(channels):
    # Блокировка не дает двум запросам одновременно строить рисунок.
    with plot_lock:
        figure = Figure(figsize=(8.8, 3.6), dpi=120, layout="constrained")
        axes = figure.subplots()
        for values, color, label in zip(channels, ("#c53939", "#2c873c", "#2764b6"),
                                        ("Красный", "Зеленый", "Синий")):
            axes.plot(range(256), values, color=color, label=label, linewidth=1.4)
        axes.set(xlabel="Значение канала", ylabel="Количество пикселей", xlim=(0, 255), ylim=(0, None))
        axes.grid(alpha=0.2)
        axes.legend(loc="upper right")
        stream = BytesIO()
        figure.savefig(stream, format="png")
        figure.clear()
        return "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode("ascii")


def show_page(error=None, result=None, status=200):
    return render_template("index.html", captcha=make_captcha(), error=error, result=result,
                           border=request.form.get("border", "30"),
                           color=request.form.get("color", "#24547a")), status


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "GET":
        return show_page()
    if not check_captcha(request.form.get("answer", "").strip()):
        return show_page("Проверка не пройдена или устарела. Решите новый пример и выберите файл еще раз.", status=400)
    try:
        value = request.form.get("border", "")
        if not re.fullmatch(r"[0-9]{1,3}", value):
            raise ValueError("Ширина рамки должна быть целым числом от 0 до 300 пикселей.")
        border = int(value)
        color = request.form.get("color", "")
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            raise ValueError("Выберите цвет рамки.")
        upload = request.files.get("image")
        if not upload or not upload.filename:
            raise ValueError("Сначала выберите изображение PNG или JPG.")
        original = read_image(upload.stream)
        framed = add_frame(original, border, color)
        histogram = histogram_url(color_histogram(original))
        preview = original.copy()
        preview.thumbnail((800, 600))
        result = {"original": png_url(preview), "framed": png_url(framed), "histogram": histogram,
                  "download": save_result(framed),
                  "width": original.width, "height": original.height,
                  "result_width": framed.width, "result_height": framed.height, "border": border}
        return show_page(result=result)
    except ValueError as error:
        return show_page(str(error), status=400)


@app.errorhandler(413)
def file_too_large(error):
    return render_template("index.html", captcha=make_captcha(),
                           error="Файл слишком большой. Выберите изображение размером до 5 МБ.",
                           result=None, border="30", color="#24547a"), 413


@app.after_request
def prevent_cache(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


if __name__ == "__main__":
    from waitress import serve
    serve(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8036")), threads=2)
