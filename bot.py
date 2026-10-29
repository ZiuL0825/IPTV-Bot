
import io
import json
import logging
import os
import re
from urllib.parse import urlparse

import httpx
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CATALOG = "sources.json"
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
MAX_CHANNELS = 200
MAX_CHECKS = 30
MAX_BYTES = 2_000_000


def load_sources():
    with open(CATALOG, encoding="utf-8") as file:
        data = json.load(file)

    sources = data.get("sources", [])
    if not isinstance(sources, list):
        raise ValueError("O campo sources deve ser uma lista.")

    return sources


def allowed_url(url, domains):
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")

        if (
            parsed.scheme not in ("http", "https")
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or not domains
        ):
            return False

        return any(
            host == domain.lower().rstrip(".")
            or host.endswith("." + domain.lower().rstrip("."))
            for domain in domains
        )
    except (ValueError, AttributeError):
        return False


def parse_m3u(content, domains):
    channels = []
    name = None

    for line in content.splitlines():
        line = line.strip()

        if line.startswith("#EXTINF:"):
            name = line.split(",", 1)[-1].strip()[:150]

        elif line and not line.startswith("#") and name is not None:
            if allowed_url(line, domains):
                channels.append({
                    "name": name or "Canal",
                    "url": line,
                })
            name = None

        if len(channels) >= MAX_CHANNELS:
            break

    unique = {}
    for channel in channels:
        unique.setdefault(channel["url"], channel)

    return list(unique.values())


async def collect_channels():
    channels = []
    errors = []

    async with httpx.AsyncClient(
        timeout=TIMEOUT,
        follow_redirects=False,
    ) as client:
        for source in load_sources():
            name = str(source.get("name", "Fonte"))[:100]
            url = source.get("url", "")
            source_domains = source.get("source_domains", [])
            stream_domains = source.get("stream_domains", [])

            if not allowed_url(url, source_domains):
                errors.append(name)
                continue

            try:
                async with client.stream("GET", url) as response:
                    if response.status_code != 200:
                        errors.append(name)
                        continue

                    chunks = []
                    size = 0

                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise ValueError("Fonte muito grande")
                        chunks.append(chunk)

                content = b"".join(chunks).decode(
                    "utf-8-sig", errors="replace"
                )

                if not content.lstrip().startswith("#EXTM3U"):
                    errors.append(name)
                    continue

                channels.extend(
                    parse_m3u(content, stream_domains)
                )

            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("Erro na fonte %s: %s", name, exc)
                errors.append(name)

    unique = {}
    for channel in channels:
        unique.setdefault(channel["url"], channel)

    return list(unique.values())[:MAX_CHANNELS], errors


async def check_url(client, url):
    try:
        async with client.stream(
            "GET",
            url,
            headers={"Range": "bytes=0-0"},
        ) as response:
            return response.status_code in (200, 206)
    except httpx.HTTPError:
        return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Olá! 📺 Sou o bot gerador de listas M3U.\n\n"
        "/gerar - gerar uma lista\n"
        "/fontes - consultar as fontes cadastradas\n"
        "/ajuda - ver os comandos"
    )


async def ajuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Uso do bot:\n"
        "1. Consulta fontes autorizadas cadastradas.\n"
        "2. Filtra os domínios permitidos.\n"
        "3. Remove URLs duplicadas.\n"
        "4. Testa até 30 transmissões.\n"
        "5. Envia o arquivo M3U.\n\n"
        "Uma resposta HTTP não garante que o vídeo reproduza."
    )


async def fontes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        items = load_sources()
        names = [
            str(source.get("name", "Fonte"))[:100]
            for source in items[:30]
        ]

        result = (
            "Fontes cadastradas:\n" + "\n".join(names)
            if names else
            "Nenhuma fonte cadastrada ainda."
        )
        await update.effective_message.reply_text(result)

    except (OSError, ValueError, json.JSONDecodeError):
        await update.effective_message.reply_text(
            "Não consegui ler sources.json."
        )


async def gerar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    await message.reply_text("Iniciando a coleta das fontes...")

    try:
        channels, errors = await collect_channels()
    except (OSError, ValueError, json.JSONDecodeError):
        logger.exception("Erro no catálogo")
        await message.reply_text(
            "Erro ao ler sources.json. Confira o arquivo."
        )
        return

    if not channels:
        await message.reply_text(
            "Nenhum canal encontrado. O catálogo está vazio "
            "ou não contém fontes válidas."
        )
        return

    valid = []

    async with httpx.AsyncClient(
        timeout=TIMEOUT,
        follow_redirects=False,
    ) as client:
        for channel in channels[:MAX_CHECKS]:
            if await check_url(client, channel["url"]):
                valid.append(channel)

    if not valid:
        await message.reply_text(
            "Nenhum dos links testados respondeu."
        )
        return

    lines = ["#EXTM3U"]

    for channel in valid:
        name = re.sub(r"[\r\n]", " ", channel["name"])
        name = name.replace('"', "'")[:150]
        lines.append(f"#EXTINF:-1,{name}")
        lines.append(channel["url"])

    output = io.BytesIO(
        ("\n".join(lines) + "\n").encode("utf-8")
    )
    output.name = "lista_iptv.m3u"

    await message.reply_document(
        document=output,
        caption=f"Lista gerada com {len(valid)} links que responderam."
        + (f"\nFontes com erro: {len(errors)}" if errors else ""),
    )


async def error_handler(update, context):
    logger.error("Erro no bot", exc_info=context.error)


def main():
    if not TOKEN:
        raise RuntimeError(
            "Configure a variável TELEGRAM_BOT_TOKEN."
        )

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ajuda", ajuda))
    app.add_handler(CommandHandler("fontes", fontes))
    app.add_handler(CommandHandler("gerar", gerar))
    app.add_error_handler(error_handler)

    logger.info("Bot iniciado.")
    app.run_polling()


if __name__ == "__main__":
    main()
          
