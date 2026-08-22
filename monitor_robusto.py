"""
Monitor robusto de nuevas fechas de "La Odisea" en IMAX Norcenter.

- Reintenta si Showcase carga incompleto.
- No manda mail por errores transitorios.
- Mantiene notified_state.json.
"""

import json
import os
import re
import smtplib
import ssl
import sys
import time
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

STATE_FILE = Path(__file__).parent / "notified_state.json"
MAX_INTENTOS = 4
ESPERA_ENTRE_INTENTOS = 8

MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5,
    "junio": 6, "julio": 7, "agosto": 8, "septiembre": 9,
    "octubre": 10, "noviembre": 11, "diciembre": 12,
}

class ErrorTransitorioShowcase(RuntimeError):
    pass

def parsear_fecha(texto):
    m = re.search(
        r"(\d{1,2})\s+de\s+([a-zA-ZáéíóúÁÉÍÓÚñÑ]+)\s+de\s+(\d{4})",
        texto, re.I
    )
    if not m:
        return None
    mes = MESES.get(m.group(2).lower())
    if not mes:
        return None
    return date(int(m.group(3)), mes, int(m.group(1)))

def cargar_estado():
    if not STATE_FILE.exists():
        return None
    data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return {"seen_dates": data}
    return data

def guardar_estado(estado):
    STATE_FILE.write_text(
        json.dumps(estado, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

def enviar_mail(asunto, cuerpo):
    if not EMAIL_FROM or not EMAIL_APP_PASSWORD or not EMAIL_TO:
        print("Faltan secrets de email.")
        return False
    try:
        msg = MIMEText(cuerpo, "plain", "utf-8")
        msg["Subject"] = asunto
        msg["From"] = EMAIL_FROM
        msg["To"] = EMAIL_TO
        with smtplib.SMTP_SSL(
            "smtp.gmail.com", 465, context=ssl.create_default_context()
        ) as server:
            server.login(EMAIL_FROM, EMAIL_APP_PASSWORD)
            server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        print("Mail enviado OK.")
        return True
    except Exception as exc:
        print(f"ERROR enviando mail: {exc}")
        return False

def esperar_select(page, selector, timeout_s=12):
    limite = time.time() + timeout_s
    while time.time() < limite:
        resultado = []
        for opcion in page.query_selector_all(f"{selector} option"):
            texto = opcion.inner_text().strip()
            value = opcion.get_attribute("value")
            if texto and value:
                resultado.append((texto, value))
        if resultado:
            return resultado
        page.wait_for_timeout(500)
    return []

def leer_una_vez(page):
    page.goto(URL_BOLETERIA, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1500)

    page.select_option(SEL_CINE, label=CINE_TEXTO)
    page.wait_for_timeout(1200)

    peliculas = esperar_select(page, SEL_PELICULA)
    if not peliculas:
        raise ErrorTransitorioShowcase(
            "El selector de películas quedó vacío después de elegir IMAX Norcenter."
        )

    patron = re.compile(re.escape(PELICULA_TEXTO), re.I)
    coincidencia = next(
        (texto for texto, _ in peliculas if patron.search(texto)), None
    )
    if coincidencia is None:
        raise RuntimeError(
            f"No se encontró '{PELICULA_TEXTO}'. "
            f"Opciones visibles: {[t for t, _ in peliculas]}"
        )

    page.select_option(SEL_PELICULA, label=coincidencia)
    page.wait_for_timeout(1200)

    formatos = esperar_select(page, SEL_FORMATO)
    if not formatos:
        raise ErrorTransitorioShowcase("El selector de formatos quedó vacío.")

    formato = next(
        (item for item in formatos if "IMAX" in item[0].upper()),
        formatos[0],
    )
    page.select_option(SEL_FORMATO, value=formato[1])
    page.wait_for_timeout(1200)

    limite = time.time() + 12
    textos_dia = []
    while time.time() < limite:
        textos_dia = [
            o.inner_text().strip()
            for o in page.query_selector_all(f"{SEL_DIA} option")
            if o.inner_text().strip()
        ]
        if textos_dia:
            break
        page.wait_for_timeout(500)

    if not textos_dia:
        raise ErrorTransitorioShowcase("El selector de días quedó vacío.")

    fechas = {}
    for texto in textos_dia:
        f = parsear_fecha(texto)
        if f:
            fechas[f.isoformat()] = texto

    if not fechas:
        raise ErrorTransitorioShowcase(
            f"No se pudieron interpretar las fechas: {textos_dia}"
        )

    return dict(sorted(fechas.items()))

def buscar_fechas():
    ultimo_error = None
    for intento in range(1, MAX_INTENTOS + 1):
        print(f"Intento {intento}/{MAX_INTENTOS}...")
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(locale="es-AR")
                fechas = leer_una_vez(page)
                browser.close()
            return fechas
        except ErrorTransitorioShowcase as exc:
            ultimo_error = exc
            print(f"Fallo transitorio: {exc}")
            if intento < MAX_INTENTOS:
                time.sleep(ESPERA_ENTRE_INTENTOS)
        except Exception:
            raise

    raise ErrorTransitorioShowcase(
        f"Showcase no respondió bien tras {MAX_INTENTOS} intentos. "
        f"Último error: {ultimo_error}"
    )

def main():
    try:
        fechas_actuales = buscar_fechas()

        print("Fechas actualmente publicadas:")
        for iso, etiqueta in fechas_actuales.items():
            print(f"  {iso} | {etiqueta}")

        estado = cargar_estado()
        actuales = set(fechas_actuales)

        if estado is None:
            guardar_estado({
                "seen_dates": sorted(actuales),
                "current_dates": sorted(actuales),
                "labels": fechas_actuales,
            })
            print("Primera ejecución: línea base creada.")
            return

        vistas = set(estado.get("seen_dates", []))
        nuevas = sorted(actuales - vistas)

        if nuevas:
            lineas = "\n".join(f"• {fechas_actuales[f]}" for f in nuevas)
            cuerpo = (
                "🚨 LA ODISEA — NUEVAS FECHAS IMAX NORCENTER\n\n"
                f"{lineas}\n\n"
                "Entrá ahora a comprar:\n"
                f"{URL_BOLETERIA}"
            )
            if not enviar_mail(
                "🚨 Nuevas fechas de La Odisea en IMAX Norcenter",
                cuerpo
            ):
                sys.exit(2)
            vistas.update(nuevas)
        else:
            print("Sin fechas nuevas.")

        estado["seen_dates"] = sorted(vistas)
        estado["current_dates"] = sorted(actuales)
        estado["labels"] = fechas_actuales
        guardar_estado(estado)
        print("Revisión completada OK.")

    except ErrorTransitorioShowcase as exc:
        print(f"AVISO TRANSITORIO: {exc}")
        print("No se modifica el estado. Se reintentará en el próximo run.")
        sys.exit(0)

    except Exception as exc:
        print(f"ERROR REAL: {exc}")
        if EMAIL_FROM and EMAIL_APP_PASSWORD and EMAIL_TO:
            enviar_mail(
                "⚠️ Falló el monitor de La Odisea",
                (
                    "El monitor tuvo un error que no parece ser una "
                    "carga transitoria de Showcase.\n\n"
                    f"Error: {repr(exc)}\n\n"
                    "Revisá GitHub Actions."
                ),
            )
        sys.exit(1)

if __name__ == "__main__":
    main()
