"""
Monitor de nuevas fechas de "La Odisea" en IMAX Norcenter.

- Lee el selector público de días de Showcase/Voy al Cine.
- Primera ejecución: guarda todas las fechas actuales como línea base y NO alerta.
- Siguientes ejecuciones: avisa sólo si aparece una fecha que nunca había visto.
- Compatible con el workflow original del repo: usa notified_state.json.
- No hace login, no reserva entradas y no toca el mapa de asientos.
"""

import json
import os
import re
import smtplib
import ssl
import sys
from datetime import date
from email.mime.text import MIMEText
from pathlib import Path

from playwright.sync_api import sync_playwright

URL_BOLETERIA = "https://www.voyalcine.net/showcase/boleteria.aspx"
CINE_TEXTO = "IMAX Theatre (Norcenter)"
PELICULA_TEXTO = "La Odisea"

SEL_CINE = "select#ctl00_Contenido_lstCinemaFull"
SEL_PELICULA = "select#ctl00_Contenido_lstMovies"
SEL_FORMATO = "select#ctl00_Contenido_lstFormat"
SEL_DIA = "select#ctl00_Contenido_lstDays"

EMAIL_FROM = os.environ.get("EMAIL_FROM")
EMAIL_APP_PASSWORD = os.environ.get("EMAIL_APP_PASSWORD")
EMAIL_TO = os.environ.get("EMAIL_TO", EMAIL_FROM)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465

# Mantiene el mismo nombre que ya guarda el workflow original.
STATE_FILE = Path(__file__).parent / "notified_state.json"

MESES = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}


def parsear_fecha(texto: str) -> date | None:
    m = re.search(
        r"(\d{1,2})\s+de\s+([a-zA-ZáéíóúÁÉÍÓÚñÑ]+)\s+de\s+(\d{4})",
        texto,
        re.I,
    )
    if not m:
        return None

    dia = int(m.group(1))
    mes = MESES.get(m.group(2).lower())
    anio = int(m.group(3))

    if not mes:
        return None

    return date(anio, mes, dia)


def cargar_estado():
    if not STATE_FILE.exists():
        return None

    data = json.loads(STATE_FILE.read_text(encoding="utf-8"))

    # Compatibilidad por si el archivo viejo era una lista.
    if isinstance(data, list):
        return {"seen_dates": data}

    return data


def guardar_estado(estado: dict):
    STATE_FILE.write_text(
        json.dumps(estado, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def enviar_mail(asunto: str, cuerpo: str) -> bool:
    if not EMAIL_FROM or not EMAIL_APP_PASSWORD or not EMAIL_TO:
        print("Faltan EMAIL_FROM, EMAIL_APP_PASSWORD o EMAIL_TO.")
        return False

    try:
        msg = MIMEText(cuerpo, "plain", "utf-8")
        msg["Subject"] = asunto
        msg["From"] = EMAIL_FROM
        msg["To"] = EMAIL_TO

        ctx = ssl.create_default_context()

        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx) as server:
            server.login(EMAIL_FROM, EMAIL_APP_PASSWORD)
            server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())

        print("Mail enviado OK.")
        return True

    except Exception as exc:
        print(f"ERROR enviando mail: {exc}")
        return False


def buscar_fechas_disponibles(page) -> dict[str, str]:
    page.goto(URL_BOLETERIA, wait_until="networkidle", timeout=60000)

    page.select_option(SEL_CINE, label=CINE_TEXTO)
    page.wait_for_timeout(1500)

    opciones_pelicula = page.query_selector_all(f"{SEL_PELICULA} option")
    textos_pelicula = [o.inner_text().strip() for o in opciones_pelicula]

    patron = re.compile(re.escape(PELICULA_TEXTO), re.I)
    coincidencia = next((t for t in textos_pelicula if patron.search(t)), None)

    if coincidencia is None:
        raise RuntimeError(
            f"No se encontró '{PELICULA_TEXTO}'. Opciones: {textos_pelicula}"
        )

    page.select_option(SEL_PELICULA, label=coincidencia)
    page.wait_for_timeout(1500)

    opciones_formato = page.query_selector_all(f"{SEL_FORMATO} option")
    formatos = []

    for opcion in opciones_formato:
        texto = opcion.inner_text().strip()
        valor = opcion.get_attribute("value")

        if texto and valor:
            formatos.append((texto, valor))

    if not formatos:
        raise RuntimeError("No se encontraron formatos disponibles.")

    formato_imax = next(
        (item for item in formatos if "IMAX" in item[0].upper()),
        formatos[0],
    )

    page.select_option(SEL_FORMATO, value=formato_imax[1])
    page.wait_for_timeout(1500)

    opciones_dia = page.query_selector_all(f"{SEL_DIA} option")
    fechas = {}

    for opcion in opciones_dia:
        texto = opcion.inner_text().strip()

        if not texto:
            continue

        fecha = parsear_fecha(texto)

        if fecha:
            fechas[fecha.isoformat()] = texto

    if not fechas:
        textos = [o.inner_text().strip() for o in opciones_dia]
        raise RuntimeError(
            f"No se pudieron leer fechas válidas. Opciones visibles: {textos}"
        )

    return dict(sorted(fechas.items()))


def main():
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(locale="es-AR")

            fechas_actuales = buscar_fechas_disponibles(page)

            browser.close()

        print("Fechas actualmente publicadas:")

        for iso, etiqueta in fechas_actuales.items():
            print(f"  {iso} | {etiqueta}")

        estado = cargar_estado()
        actuales = set(fechas_actuales.keys())

        # Primera ejecución: crear línea base sin alertar.
        if estado is None:
            guardar_estado(
                {
                    "seen_dates": sorted(actuales),
                    "current_dates": sorted(actuales),
                    "labels": fechas_actuales,
                }
            )

            print("Primera ejecución: línea base creada. No se envía alerta.")
            return

        vistas = set(estado.get("seen_dates", []))
        nuevas = sorted(actuales - vistas)

        if nuevas:
            lineas = "\n".join(
                f"• {fechas_actuales[fecha]}" for fecha in nuevas
            )

            cuerpo = (
                "🚨 LA ODISEA — NUEVAS FECHAS IMAX NORCENTER\n\n"
                f"{lineas}\n\n"
                "Entrá ahora a comprar:\n"
                f"{URL_BOLETERIA}"
            )

            print(f"Nuevas fechas detectadas: {nuevas}")

            if not enviar_mail(
                "🚨 Nuevas fechas de La Odisea en IMAX Norcenter",
                cuerpo,
            ):
                # No marcar como vistas si el mail falló.
                estado["current_dates"] = sorted(actuales)
                estado["labels"] = fechas_actuales
                guardar_estado(estado)
                sys.exit(2)

            vistas.update(nuevas)

        else:
            print("Sin fechas nuevas.")

        estado["seen_dates"] = sorted(vistas)
        estado["current_dates"] = sorted(actuales)
        estado["labels"] = fechas_actuales

        guardar_estado(estado)

        print("Revisión completada OK.")

    except Exception as exc:
        print(f"ERROR: {exc}")

        if EMAIL_FROM and EMAIL_APP_PASSWORD and EMAIL_TO:
            enviar_mail(
                "⚠️ Falló el monitor de La Odisea",
                (
                    "El monitor de La Odisea en IMAX Norcenter falló.\n\n"
                    f"Error: {repr(exc)}\n\n"
                    "Revisá GitHub Actions."
                ),
            )

        sys.exit(1)


if __name__ == "__main__":
    main()
