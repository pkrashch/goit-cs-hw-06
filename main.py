"""
main.py

Найпростіший вебдодаток на Python без використання вебфреймворків.

Складається з двох частин, які запускаються як окремі процеси
(multiprocessing.Process):

1. HTTP-сервер (http.server, порт 3000) — маршрутизація для index.html та
   message.html, віддача статичних ресурсів (style.css, logo.png), 404 ->
   error.html, прийом даних форми через POST-запит.
2. Socket-сервер (UDP, порт 5000) — приймає байт-рядок з даними форми від
   HTTP-сервера, перетворює його на словник і зберігає документ у MongoDB
   у форматі {"date": ..., "username": ..., "message": ...}.

HTTP-сервер, отримавши POST-запит з формою, не звертається до MongoDB
напряму: він пересилає сирі дані форми Socket-серверу по UDP, а вже
Socket-сервер відповідає за перетворення даних і запис у базу.
"""

import logging
import multiprocessing
import os
import socket
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pymongo

BASE_DIR = Path(__file__).parent

HTTP_HOST = "0.0.0.0"
HTTP_PORT = 3000

SOCKET_HOST = "127.0.0.1"
SOCKET_PORT = 5000
BUFFER_SIZE = 1024

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.environ.get("MONGO_DB", "messages_db")
MONGO_COLLECTION = os.environ.get("MONGO_COLLECTION", "messages")

STATIC_FILES = {
    "/style.css": ("style.css", "text/css"),
    "/logo.png": ("logo.png", "image/png"),
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(processName)s] [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


class HttpHandler(BaseHTTPRequestHandler):
    """Обробник HTTP-запитів: маршрутизація сторінок, статика, форма."""

    def do_GET(self):
        route = urllib.parse.urlparse(self.path).path

        if route == "/":
            self.send_html_file("index.html")
        elif route in ("/message", "/message.html"):
            self.send_html_file("message.html")
        elif route in STATIC_FILES:
            filename, content_type = STATIC_FILES[route]
            self.send_static_file(filename, content_type)
        else:
            self.send_html_file("error.html", status=404)

    def do_POST(self):
        route = urllib.parse.urlparse(self.path).path

        if route == "/message":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            self.forward_to_socket_server(body)

            self.send_response(302)
            self.send_header("Location", "/message.html")
            self.end_headers()
        else:
            self.send_html_file("error.html", status=404)

    def forward_to_socket_server(self, data: bytes) -> None:
        """Пересилає сирі байти форми Socket-серверу через UDP."""
        client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            client_socket.sendto(data, (SOCKET_HOST, SOCKET_PORT))
        except OSError as e:
            logger.error("Не вдалося надіслати дані Socket-серверу: %s", e)
        finally:
            client_socket.close()

    def send_html_file(self, filename: str, status: int = 200) -> None:
        filepath = BASE_DIR / filename
        try:
            with open(filepath, "rb") as f:
                content = f.read()
        except OSError as e:
            logger.error("Не вдалося прочитати %s: %s", filepath, e)
            self.send_response(500)
            self.end_headers()
            return

        self.send_response(status)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(content)

    def send_static_file(self, filename: str, content_type: str) -> None:
        filepath = BASE_DIR / filename
        try:
            with open(filepath, "rb") as f:
                content = f.read()
        except OSError:
            self.send_html_file("error.html", status=404)
            return

        self.send_response(200)
        self.send_header("Content-type", content_type)
        self.end_headers()
        self.wfile.write(content)

    def address_string(self) -> str:
        # Стандартна реалізація робить зворотний DNS-пошук (socket.getfqdn),
        # який у Docker-мережі може "зависати" на кожному запиті. Тут
        # достатньо просто IP-адреси клієнта, без резолвінгу імені.
        return self.client_address[0]

    def log_message(self, format, *args):  # noqa: A002 (перевизначення BaseHTTPRequestHandler)
        logger.info("%s - %s", self.address_string(), format % args)


def run_http_server() -> None:
    server = HTTPServer((HTTP_HOST, HTTP_PORT), HttpHandler)
    logger.info("HTTP-сервер запущено на http://%s:%d", HTTP_HOST, HTTP_PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def parse_form_data(raw: bytes) -> dict:
    """Перетворює url-encoded байт-рядок форми у звичайний словник."""
    decoded = raw.decode("utf-8")
    parsed = urllib.parse.parse_qs(decoded)
    return {key: values[0] for key, values in parsed.items() if values}


def save_message(document: dict) -> None:
    """Зберігає документ повідомлення у MongoDB."""
    client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    try:
        collection = client[MONGO_DB][MONGO_COLLECTION]
        collection.insert_one(document)
        logger.info("Збережено повідомлення в MongoDB: %s", document)
    except pymongo.errors.PyMongoError as e:
        logger.error("Помилка запису в MongoDB: %s", e)
    finally:
        client.close()


def run_socket_server() -> None:
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server_socket.bind((SOCKET_HOST, SOCKET_PORT))
    logger.info("Socket-сервер (UDP) запущено на %s:%d", SOCKET_HOST, SOCKET_PORT)

    try:
        while True:
            data, address = server_socket.recvfrom(BUFFER_SIZE)
            try:
                form_data = parse_form_data(data)
                document = {
                    "date": str(datetime.now()),
                    "username": form_data.get("username", ""),
                    "message": form_data.get("message", ""),
                }
                save_message(document)
            except (UnicodeDecodeError, ValueError) as e:
                logger.error("Некоректні дані форми від %s: %s", address, e)
    except KeyboardInterrupt:
        pass
    finally:
        server_socket.close()


def main() -> None:
    http_process = multiprocessing.Process(target=run_http_server, name="HTTP-server")
    socket_process = multiprocessing.Process(target=run_socket_server, name="Socket-server")

    http_process.start()
    socket_process.start()

    try:
        http_process.join()
        socket_process.join()
    except KeyboardInterrupt:
        http_process.terminate()
        socket_process.terminate()


if __name__ == "__main__":
    main()
