#!/usr/bin/env python3
"""Monitor de agenda: revisa la web de reservas, lee los días disponibles por
profesional en las próximas semanas y avisa por push (ntfy) si hay cupos.

Determinístico, sin LLM en runtime. Ver SPEC.md para el detalle.
"""

import os
import ssl
import sys
import urllib.request
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

# --- Selectores estructurales (NO son secretos: son clases genéricas del
# widget Reservo). Lo sensible - URL y servicios - se lee desde el entorno. ---
SEL_OPCION = ".stf-select-option"                 # cada item de un desplegable
SEL_VALUE = ".stf-select__inner-wrapper"           # zona clickeable de un select
SEL_CONTINUAR = "button.primary-custom-color:has-text('Continuar'):visible"
SEL_DIA = ".custom-calendar-day-slot"              # cada celda de día del calendario
SEL_DIA_NUM = ".text-lg"                            # el número dentro de la celda
SEL_MES = "#vs1__combobox .vs__selected"           # "Agosto 2026" en el dropdown de mes

MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10, "noviembre": 11,
    "diciembre": 12,
}
MESES_ABR = ["", "ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep",
             "oct", "nov", "dic"]
DIAS_ABR = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]


def log(msg: str, nivel: str = "INFO") -> None:
    print(f"{datetime.now().isoformat(timespec='seconds')} [{nivel}] {msg}", flush=True)


@dataclass
class Config:
    url: str
    categoria_id: str
    intervencion_id: str
    topic: str
    semanas: int
    headless: bool


def cargar_config() -> Config:
    """Lee la configuración del entorno. Falla claro si falta algo."""
    faltan = [k for k in ("MONITOR_URL", "MONITOR_CATEGORIA_ID",
                          "MONITOR_INTERVENCION_ID", "NTFY_TOPIC")
              if not os.environ.get(k)]
    if faltan:
        raise SystemExit(f"Faltan variables de entorno: {', '.join(faltan)}")
    return Config(
        url=os.environ["MONITOR_URL"],
        categoria_id=os.environ["MONITOR_CATEGORIA_ID"],
        intervencion_id=os.environ["MONITOR_INTERVENCION_ID"],
        topic=os.environ["NTFY_TOPIC"],
        semanas=int(os.environ.get("MONITOR_SEMANAS", "4")),
        headless=os.environ.get("MONITOR_HEADLESS", "1") != "0",
    )


# --------------------------------------------------------------------------- #
# Navegación
# --------------------------------------------------------------------------- #
def _abrir_select(page, etiqueta: str) -> None:
    """Abre un desplegable stf-select por el texto de su placeholder y espera
    a que sus opciones queden visibles.

    Los 3 selects tienen su contenedor de opciones en el DOM siempre; el abierto
    es el único con `visibility: visible`. Por eso filtramos con `:visible`
    (Playwright respeta visibility:hidden), que aísla el dropdown abierto.
    """
    page.locator(".stf-select", has_text=etiqueta).locator(SEL_VALUE).first.click()
    page.locator(f"{SEL_OPCION}:visible").first.wait_for(timeout=8_000)


def _click_continuar(page) -> None:
    page.locator(SEL_CONTINUAR).first.click()
    page.wait_for_timeout(500)


def _navegar_a_profesionales(page, cfg: Config) -> None:
    """Pasos 1-6: abre la web y deja el desplegable de profesionales abierto."""
    page.goto(cfg.url, wait_until="networkidle", timeout=30_000)
    _abrir_select(page, "Selecciona Categoría")
    page.locator(f'{SEL_OPCION}[data-testid="{cfg.categoria_id}"]').click()
    _abrir_select(page, "Selecciona Intervención")
    page.locator(f'{SEL_OPCION}[data-testid="{cfg.intervencion_id}"]').click()
    _click_continuar(page)
    _abrir_select(page, "Selecciona Profesional")


def listar_profesionales(page, cfg: Config) -> list[dict]:
    """Devuelve [{testid, nombre}] de todos los profesionales del desplegable."""
    _navegar_a_profesionales(page, cfg)
    opciones = page.locator(f"{SEL_OPCION}:visible")   # solo el dropdown abierto
    opciones.first.wait_for(timeout=10_000)
    profs = []
    for i in range(opciones.count()):
        el = opciones.nth(i)
        profs.append({
            "testid": el.get_attribute("data-testid"),
            "nombre": (el.inner_text() or "").strip(),
        })
    # No logueamos los nombres: el repo es público y los logs de Actions también.
    log(f"Profesionales encontrados: {len(profs)}")
    return profs


# --------------------------------------------------------------------------- #
# Lectura del calendario
# --------------------------------------------------------------------------- #
def _mes_dropdown(page) -> tuple[int, int]:
    """Lee el mes/año del selector (ej. 'Agosto 2026' -> (8, 2026))."""
    txt = page.locator(SEL_MES).first.inner_text().strip().lower()
    partes = txt.split()
    return MESES[partes[0]], int(partes[1])


def _dias_disponibles_en_vista(page, mes: int, anio: int) -> list[date]:
    """Extrae las fechas de las celdas seleccionables del calendario cargado.

    Disponible = celda con `cursor-pointer` y SIN `cursor-not-allowed`.

    FLAG FASE 2: el widget muestra días del mes SIGUIENTE como seleccionables
    con el dropdown todavía en el mes actual (visto en el HTML: '1' y '4' de
    septiembre con cursor-pointer mientras el selector decía 'Agosto'). El
    fechado usa rollover cuando el número de día baja tras fin de mes. Hay que
    validarlo en vivo con headless=False antes de confiar en el borde de mes.
    """
    fechas: list[date] = []
    celdas = page.locator(SEL_DIA)
    m, a, prev = mes, anio, 0
    for i in range(celdas.count()):
        cel = celdas.nth(i)
        num = cel.locator(SEL_DIA_NUM).first
        if not num.count():
            continue
        try:
            dia = int(num.inner_text().strip())
        except ValueError:
            continue
        if prev and dia < prev and prev >= 20:   # cruzó a mes siguiente
            m += 1
            if m > 12:
                m, a = 1, a + 1
        prev = dia
        clases = cel.get_attribute("class") or ""
        disponible = "cursor-pointer" in clases and "cursor-not-allowed" not in clases
        if disponible:
            try:
                fechas.append(date(a, m, dia))
            except ValueError:
                pass
    return fechas


def _avanzar_mes(page) -> bool:
    """Avanza al mes siguiente en el selector de mes. Devuelve False si no puede.

    FLAG FASE 2: la navegación de mes hay que confirmarla en vivo (el vue-select
    #vs1 puede requerir otra interacción). Para una ventana de 4 semanas a
    principio de mes normalmente NO se necesita: la última semana del mes actual
    ya muestra los primeros días del siguiente. Igual se deja por robustez.
    """
    combo = page.locator("#vs1__combobox")
    if not combo.count():
        return False
    combo.click()
    page.wait_for_timeout(300)
    opciones = page.locator("#vs1__listbox li")
    if not opciones.count():
        page.keyboard.press("Escape")
        return False
    idx = 1 if opciones.count() > 1 else 0
    opciones.nth(idx).click()
    page.wait_for_timeout(500)
    return True


def _dump_debug(page, etiqueta: str) -> None:
    """Guarda screenshot en MONITOR_DEBUG_DIR si está seteado (solo para debug)."""
    carpeta = os.environ.get("MONITOR_DEBUG_DIR")
    if not carpeta:
        return
    try:
        ruta = os.path.join(carpeta, f"debug_{etiqueta}.png")
        page.screenshot(path=ruta, full_page=True)
        log(f"Screenshot de debug: {ruta}")
    except Exception as e:   # nunca romper el flujo por el debug
        log(f"No se pudo guardar screenshot: {e}", "WARN")


def revisar_web(cfg: Config) -> dict:
    """Abre la web, itera cada profesional y junta sus días disponibles.

    Devuelve {"ok": True, "agenda": {nombre: [fechas]}} o {"ok": False, "error"}.
    """
    hoy = date.today()
    fin = hoy + timedelta(days=cfg.semanas * 7)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=cfg.headless)
        page = browser.new_page()
        try:
            profesionales = listar_profesionales(page, cfg)
            agenda: dict[str, list[date]] = {}
            for i, prof in enumerate(profesionales, start=1):
                etiqueta = f"Profesional {i}"   # nunca el nombre real (privacidad)
                _navegar_a_profesionales(page, cfg)
                page.locator(f'{SEL_OPCION}[data-testid="{prof["testid"]}"]').click()
                _click_continuar(page)
                page.wait_for_selector(SEL_DIA, timeout=15_000)

                fechas: list[date] = []
                for _ in range(cfg.semanas // 4 + 2):   # tope de meses a revisar
                    mes, anio = _mes_dropdown(page)
                    fechas += _dias_disponibles_en_vista(page, mes, anio)
                    if date(anio, mes, monthrange(anio, mes)[1]) >= fin:
                        break
                    if not _avanzar_mes(page):
                        break

                dias = sorted({f for f in fechas if hoy <= f <= fin})
                agenda[etiqueta] = dias
                log(f"{etiqueta}: {len(dias)} día(s) disponible(s) en la ventana")
            return {"ok": True, "agenda": agenda}
        except PlaywrightTimeoutError as e:
            _dump_debug(page, "timeout")
            return {"ok": False, "error": f"timeout de navegación: {e}"}
        finally:
            browser.close()


# --------------------------------------------------------------------------- #
# Regla de negocio y aviso
# --------------------------------------------------------------------------- #
def _fmt_fecha(f: date) -> str:
    return f"{DIAS_ABR[f.weekday()]} {f.day} {MESES_ABR[f.month]}"


def amerita_aviso(estado: dict) -> tuple[bool, str]:
    """Decide si hay que avisar y arma el mensaje. Toda la regla vive acá."""
    agenda = estado.get("agenda", {})
    con_cupo = {prof: fechas for prof, fechas in agenda.items() if fechas}
    if not con_cupo:
        return False, "sin novedad: ningún profesional con cupos en la ventana"
    lineas = ["Cupos disponibles (próx. 4 semanas):"]
    for prof, fechas in con_cupo.items():
        lineas.append(f"- {prof}: {', '.join(_fmt_fecha(f) for f in fechas)}")
    return True, "\n".join(lineas)


def _contexto_ssl() -> ssl.SSLContext | None:
    """Usa el bundle de certifi si está instalado (Mac local); si no, cae a los
    certs del sistema (Ubuntu de GitHub Actions). Evita el clásico
    CERTIFICATE_VERIFY_FAILED del Python de python.org sin sumar dependencias
    duras en el runner."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return None


def enviar_aviso(topic: str, mensaje: str) -> None:
    """POST del mensaje al topic de ntfy. El topic se lee del entorno (secreto)."""
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}",
        data=mensaje.encode("utf-8"),
        headers={
            "Title": "Monitor de agenda",
            "Priority": "high",
            "Tags": "calendar",
        },
        method="POST",
    )
    urllib.request.urlopen(req, timeout=10, context=_contexto_ssl())


def main() -> None:
    cfg = cargar_config()
    log("Iniciando revisión de agenda")
    estado = revisar_web(cfg)
    if not estado["ok"]:
        log(f"Falló la navegación: {estado['error']}", "ERROR")
        sys.exit(1)
    aviso, mensaje = amerita_aviso(estado)
    if aviso:
        log("Amerita aviso, enviando push")
        enviar_aviso(cfg.topic, mensaje)
        log("Push enviado")
    else:
        log(mensaje)
    sys.exit(0)


if __name__ == "__main__":
    main()
