import base64
import re
import time
import unittest
from io import BytesIO
from unittest.mock import patch

from PIL import Image
from werkzeug.test import EnvironBuilder
import app as module


def image_file(size=(8, 6), color=(10, 20, 30), mode="RGB", fmt="PNG"):
    data = BytesIO()
    Image.new(mode, size, color).save(data, format=fmt)
    data.seek(0)
    return data


class FrameTests(unittest.TestCase):
    def test_size_and_center(self):
        source = Image.new("RGB", (8, 6), (10, 20, 30))
        result = module.add_frame(source, 3, "#ff0000")
        self.assertEqual(result.size, (14, 12))
        self.assertEqual(result.getpixel((0, 0)), (255, 0, 0))
        self.assertEqual(result.crop((3, 3, 11, 9)).tobytes(), source.tobytes())
        self.assertEqual(source.size, (8, 6))

    def test_zero_border(self):
        source = Image.new("RGB", (4, 3), "blue")
        result = module.add_frame(source, 0, "red")
        self.assertEqual(source.tobytes(), result.tobytes())
        self.assertEqual(source.size, result.size)

    def test_maximum_border(self):
        self.assertEqual(module.add_frame(Image.new("RGB", (1, 1)), 300, "black").size, (601, 601))

    def test_bad_width(self):
        for width in (-1, 301):
            with self.subTest(width=width), self.assertRaises(ValueError):
                module.add_frame(Image.new("RGB", (2, 2)), width, "black")

    def test_result_size_limit(self):
        with self.assertRaises(ValueError):
            module.add_frame(Image.new("RGB", (8000, 500)), 300, "black")

    def test_histogram_known_pixels(self):
        source = Image.new("RGB", (2, 2))
        source.putdata([(255, 0, 0), (255, 0, 0), (0, 255, 0), (0, 0, 255)])
        red, green, blue = module.color_histogram(source)
        self.assertEqual((red[255], green[255], blue[255]), (2, 1, 1))
        self.assertEqual((red[0], green[0], blue[0]), (2, 3, 3))
        self.assertTrue(all(len(c) == 256 and sum(c) == 4 for c in (red, green, blue)))

    def test_transparency(self):
        source = module.read_image(image_file((2, 2), (255, 0, 0, 0), "RGBA"))
        self.assertEqual(source.mode, "RGB")
        self.assertEqual(source.getpixel((0, 0)), (255, 255, 255))

    def test_jpeg_rotation(self):
        data = BytesIO()
        image = Image.new("RGB", (8, 6), "blue")
        exif = Image.Exif()
        exif[274] = 6
        image.save(data, format="JPEG", exif=exif)
        data.seek(0)
        self.assertEqual(module.read_image(data).size, (6, 8))

    def test_not_an_image(self):
        with self.assertRaises(ValueError):
            module.read_image(BytesIO(b"not an image"))

    def test_unsupported_format(self):
        with self.assertRaises(ValueError):
            module.read_image(image_file(fmt="BMP"))

    def test_input_size_limit(self):
        with self.assertRaises(ValueError):
            module.read_image(image_file((2001, 2000)))


class PageTests(unittest.TestCase):
    def setUp(self):
        module.app.config.update(TESTING=True, SECRET_KEY="test-secret-only")
        module.used_captchas.clear()
        self.client = module.app.test_client()

    def challenge(self):
        # В тесте получаем известный пример 6 + 6. В приложении числа случайные.
        with patch("app.secrets.randbelow", return_value=4):
            return self.client.get("/")

    def submit(self, **changes):
        self.challenge()
        data = {"answer": "12", "border": "3", "color": "#ff0000",
                "image": (image_file(), "sample.png")}
        data.update(changes)
        return self.client.post("/", data=data)

    def test_start_page(self):
        response = self.challenge()
        self.assertEqual(response.status_code, 200)
        self.assertIn("Добавление рамки к изображению", response.get_data(as_text=True))
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_success_and_download(self):
        response = self.submit()
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn("14 × 12", text)
        self.assertIn("Распределение цветов исходного изображения", text)
        link = re.search(r'href="(/download/[0-9a-f]+)"', text).group(1)
        downloaded = self.client.get(link)
        self.assertEqual(downloaded.status_code, 200)
        self.assertIn("attachment", downloaded.headers["Content-Disposition"])
        result = Image.open(BytesIO(downloaded.data))
        self.assertEqual(result.size, (14, 12))
        self.assertEqual(result.getpixel((0, 0)), (255, 0, 0))
        self.assertEqual(result.getpixel((3, 3)), (10, 20, 30))
        downloaded.close()

    def test_private_download(self):
        response = self.submit()
        link = re.search(r'href="(/download/[0-9a-f]+)"', response.get_data(as_text=True)).group(1)
        other = module.app.test_client()
        self.assertEqual(other.get(link).status_code, 404)

    def test_bad_download_path(self):
        self.assertEqual(self.client.get("/download/not-a-file").status_code, 404)

    def test_wrong_captcha(self):
        response = self.submit(answer="99")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Проверка не пройдена", response.get_data(as_text=True))
        self.assertNotIn("Обработка завершена", response.get_data(as_text=True))

    def test_captcha_reuse(self):
        self.challenge()
        with self.client.session_transaction() as session:
            old = dict(session)
        self.client.post("/", data={"answer": "12", "image": (image_file(), "first.png"),
                                   "border": "3", "color": "#ff0000"})
        with self.client.session_transaction() as session:
            session.clear()
            session.update(old)
        response = self.client.post("/", data={"answer": "12", "image": (image_file(), "second.png"),
                                               "border": "3", "color": "#ff0000"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Проверка не пройдена", response.get_data(as_text=True))

    def test_expired_captcha(self):
        self.challenge()
        with self.client.session_transaction() as session:
            data = dict(session["captcha"])
            data["created"] = time.time() - 301
            session["captcha"] = data
        response = self.client.post("/", data={"answer": "12"})
        self.assertEqual(response.status_code, 400)

    def test_missing_file(self):
        response = self.submit(image=(BytesIO(), ""))
        self.assertEqual(response.status_code, 400)
        self.assertIn("Сначала выберите", response.get_data(as_text=True))

    def test_bad_file(self):
        response = self.submit(image=(BytesIO(b"broken"), "fake.png"))
        self.assertEqual(response.status_code, 400)
        self.assertIn("Не удалось открыть", response.get_data(as_text=True))

    def test_invalid_widths(self):
        for value in ("-1", "301", "1.5", "", "abc"):
            with self.subTest(value=value):
                response = self.submit(border=value)
                self.assertEqual(response.status_code, 400)
                self.assertIn("Ширина рамки", response.get_data(as_text=True))

    def test_invalid_color(self):
        response = self.submit(color="red")
        self.assertEqual(response.status_code, 400)

    def test_http_upload_limit(self):
        self.challenge()
        builder = EnvironBuilder(path="/", method="POST", data={"image": (BytesIO(b"x" * (5 * 1024 * 1024)), "big.png")})
        environ = builder.get_environ()
        try:
            response = self.client.open(environ)
        finally:
            environ["wsgi.input"].close()
            builder.close()
        self.assertEqual(response.status_code, 413)
        self.assertIn("Файл слишком большой", response.get_data(as_text=True))

    def test_html_in_filename_not_rendered(self):
        response = self.submit(image=(image_file(), '<script>alert(1)</script>.png'))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('<script>alert(1)</script>', response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
