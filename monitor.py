#!/usr/bin/env python3
"""Monitor de agenda: revisa la web de reservas, lee los días disponibles por
profesional en las próximas semanas y avisa por push (ntfy) si hay cupos.

Determinístico, sin LLM en runtime. Ver SPEC.md para el detalle.
"""

import json
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
SEL_MES_WRAP = ".select-reservo-unique"            # wrapper del vue-select de mes
SEL_MES = f"{SEL_MES_WRAP} .vs__selected"          # "Agosto 2026" (id vsN es dinámico)

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
    flows: list[dict]      # [{"cat", "int", "label"}, ...]
    topic: str
    semanas: int
    headless: bool
    resumen_min: int       # minutos entre resúmenes horarios forzados


def cargar_config() -> Config:
    """Lee la configuración del entorno. Falla claro si falta algo.

    MONITOR_FLOWS es un JSON con la lista de flujos a revisar. Cada flujo tiene
    `cat` (data-testid de la categoría), `int` (data-testid de la intervención)
    y `label` (etiqueta neutra para la notificación; NO poner info de salud, ver
    README). Ej:
      [{"cat":"20022","int":"371434","label":"Servicio 1"}, ...]
    """
    faltan = [k for k in ("MONITOR_URL", "MONITOR_FLOWS", "NTFY_TOPIC")
              if not os.environ.get(k)]
    if faltan:
        raise SystemExit(f"Faltan variables de entorno: {', '.join(faltan)}")
    try:
        flows = json.loads(os.environ["MONITOR_FLOWS"])
    except json.JSONDecodeError as e:
        raise SystemExit(f"MONITOR_FLOWS no es JSON válido: {e}")
    if not isinstance(flows, list) or not flows:
        raise SystemExit("MONITOR_FLOWS debe ser una lista no vacía de flujos")
    for f in flows:
        if not all(k in f for k in ("cat", "int", "label")):
            raise SystemExit(f"Flujo incompleto (faltan cat/int/label): {f}")
    return Config(
        url=os.environ["MONITOR_URL"],
        flows=flows,
        topic=os.environ["NTFY_TOPIC"],
        semanas=int(os.environ.get("MONITOR_SEMANAS", "4")),
        headless=os.environ.get("MONITOR_HEADLESS", "1") != "0",
        resumen_min=int(os.environ.get("MONITOR_RESUMEN_MIN", "60")),
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


def _navegar_a_profesionales(page, url: str, cat_id: str, int_id: str) -> None:
    """Pasos 1-6: abre la web para un flujo (categoría + intervención) y deja el
    desplegable de profesionales abierto."""
    page.goto(url, wait_until="networkidle", timeout=30_000)
    _abrir_select(page, "Selecciona Categoría")
    page.locator(f'{SEL_OPCION}[data-testid="{cat_id}"]').click()
    _abrir_select(page, "Selecciona Intervención")
    page.locator(f'{SEL_OPCION}[data-testid="{int_id}"]').click()
    _click_continuar(page)
    _abrir_select(page, "Selecciona Profesional")


def listar_profesionales(page, url: str, cat_id: str, int_id: str) -> list[dict]:
    """Devuelve [{testid, nombre}] de todos los profesionales del flujo."""
    _navegar_a_profesionales(page, url, cat_id, int_id)
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
    """Lee el mes/año del selector (ej. 'Agosto 2026' -> (8, 2026)).

    Algunos calendarios (ej. un profesional sin disponibilidad) muestran el
    picker vacío, sin `.vs__selected`. En ese caso cae al mes actual como base:
    es irrelevante porque si el picker está vacío no hay días seleccionables.
    """
    loc = page.locator(SEL_MES)
    try:
        if loc.count():
            txt = (loc.first.inner_text(timeout=3_000) or "").strip().lower()
            if txt:
                partes = txt.split()
                return MESES[partes[0]], int(partes[1])
    except Exception:
        pass
    hoy = date.today()
    log("Picker de mes vacío; uso el mes actual como base", "WARN")
    return hoy.month, hoy.year


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
    combo = page.locator(f"{SEL_MES_WRAP} .vs__dropdown-toggle")
    if not combo.count():
        return False
    combo.first.click()
    page.wait_for_timeout(300)
    opciones = page.locator(f"{SEL_MES_WRAP} ul[role=listbox] li")
    if not opciones.count():
        page.keyboard.press("Escape")
        return False
    idx = 1 if opciones.count() > 1 else 0
    opciones.nth(idx).click()
    page.wait_for_timeout(500)
    return True


def _leer_agenda_calendario(page, hoy: date, fin: date, semanas: int) -> list[date]:
    """Lee los días disponibles del calendario ya cargado, dentro de la ventana."""
    fechas: list[date] = []
    for _ in range(semanas // 4 + 2):   # tope de meses a revisar
        mes, anio = _mes_dropdown(page)
        fechas += _dias_disponibles_en_vista(page, mes, anio)
        if date(anio, mes, monthrange(anio, mes)[1]) >= fin:
            break
        if not _avanzar_mes(page):
            break
    return sorted({f for f in fechas if hoy <= f <= fin})


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
    """Abre la web, itera cada flujo y, dentro de cada uno, cada profesional.

    Devuelve {"ok": True, "agenda": {label_flujo: {"Profesional N": [fechas]}}}
    o {"ok": False, "error": str} si falla la navegación.
    """
    hoy = date.today()
    fin = hoy + timedelta(days=cfg.semanas * 7)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=cfg.headless)
        page = browser.new_page()
        try:
            agenda: dict[str, dict[str, list[date]]] = {}
            for fi, flow in enumerate(cfg.flows, start=1):
                label = flow["label"]
                profs = listar_profesionales(page, cfg.url, flow["cat"], flow["int"])
                por_prof: dict[str, list[date]] = {}
                for i, prof in enumerate(profs, start=1):
                    _navegar_a_profesionales(page, cfg.url, flow["cat"], flow["int"])
                    page.locator(f'{SEL_OPCION}[data-testid="{prof["testid"]}"]').click()
                    _click_continuar(page)
                    page.wait_for_selector(SEL_DIA, timeout=15_000)
                    dias = _leer_agenda_calendario(page, hoy, fin, cfg.semanas)
                    por_prof[f"Profesional {i}"] = dias   # nunca el nombre real
                    # Log por índice, no por label: los logs de Actions son
                    # públicos y el label puede tener el nombre real del servicio.
                    log(f"Flujo {fi} / Profesional {i}: {len(dias)} día(s) en la ventana")
                agenda[label] = por_prof
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


def _cuerpo_por_prof(por_prof: dict) -> str:
    """Arma las líneas '- Profesional N: fechas' de los que tienen cupo."""
    return "\n".join(
        f"- {prof}: {', '.join(_fmt_fecha(f) for f in fechas)}"
        for prof, fechas in por_prof.items() if fechas
    )


def _nuevos_cupos(prev_agenda: dict, agenda: dict) -> dict:
    """Devuelve {servicio: {'Profesional N': [fechas nuevas]}} con SOLO los días
    que aparecieron respecto a la corrida anterior (no los que se ocuparon)."""
    nuevos: dict[str, dict[str, list[date]]] = {}
    for svc, por_prof in agenda.items():
        prev_prof = (prev_agenda or {}).get(svc, {})
        for prof, fechas in por_prof.items():
            antes = set(prev_prof.get(prof, []))
            agregados = [f for f in fechas if f.isoformat() not in antes]
            if agregados:
                nuevos.setdefault(svc, {})[prof] = agregados
    return nuevos


def _agenda_a_json(agenda: dict) -> dict:
    return {svc: {prof: [f.isoformat() for f in fechas]
                  for prof, fechas in por_prof.items()}
            for svc, por_prof in agenda.items()}


# --------------------------------------------------------------------------- #
# Estado persistente (entre corridas, vía cache de Actions)
# --------------------------------------------------------------------------- #
def _ruta_estado() -> str:
    return os.environ.get("MONITOR_ESTADO", "estado.json")


def cargar_estado() -> dict | None:
    ruta = _ruta_estado()
    if not os.path.exists(ruta):
        return None
    try:
        with open(ruta, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def guardar_estado(agenda: dict, ultimo_resumen: datetime) -> None:
    with open(_ruta_estado(), "w", encoding="utf-8") as f:
        json.dump({"agenda": _agenda_a_json(agenda),
                   "ultimo_resumen": ultimo_resumen.isoformat()},
                  f, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- #
# Envío ntfy
# --------------------------------------------------------------------------- #
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


def enviar_aviso(topic: str, mensaje: str, titulo: str = "Monitor de agenda",
                 prioridad: str = "high", tags: str = "calendar") -> None:
    """POST del mensaje al topic de ntfy. El título va en ASCII (header HTTP)."""
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}",
        data=mensaje.encode("utf-8"),
        headers={"Title": titulo, "Priority": prioridad, "Tags": tags},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=10, context=_contexto_ssl())


def enviar_alertas_nuevos(cfg: Config, nuevos: dict) -> None:
    """Una alerta URGENTE por servicio con cupos nuevos (título = servicio)."""
    for svc, por_prof in nuevos.items():
        cuerpo = _cuerpo_por_prof(por_prof)
        if not cuerpo:
            continue
        log(f"Alerta de cupo nuevo: {svc}")
        enviar_aviso(cfg.topic, cuerpo, titulo=f"Nuevo cupo: {svc}",
                     prioridad="urgent", tags="rotating_light")


def enviar_resumen(cfg: Config, agenda: dict) -> None:
    """Resumen horario: una sola notificación con todos los servicios."""
    bloques = []
    for svc, por_prof in agenda.items():
        cuerpo = _cuerpo_por_prof(por_prof)
        if cuerpo:
            bloques.append(f"[{svc}]\n{cuerpo}")
    if bloques:
        mensaje = "Disponible ahora:\n" + "\n".join(bloques)
    else:
        mensaje = "Sin cupos disponibles en la ventana por ahora."
    log("Enviando resumen horario")
    enviar_aviso(cfg.topic, mensaje, titulo="Resumen agenda",
                 prioridad="default", tags="calendar")


def main() -> None:
    cfg = cargar_config()
    log("Iniciando revisión de agenda")
    estado = revisar_web(cfg)
    if not estado["ok"]:
        log(f"Falló la navegación: {estado['error']}", "ERROR")
        sys.exit(1)
    agenda = estado["agenda"]
    prev = cargar_estado()
    ahora = datetime.now()
    forzar_resumen = os.environ.get("MONITOR_FORZAR_RESUMEN") == "1"

    if prev is None:
        # Primera corrida (sin estado): resumen base, sin alertas de "nuevo"
        # para no disparar todo lo disponible de golpe.
        log("Sin estado previo: resumen base, sin alertas de cupos nuevos")
        enviar_resumen(cfg, agenda)
        ultimo_resumen = ahora
    else:
        nuevos = _nuevos_cupos(prev.get("agenda", {}), agenda)
        if nuevos:
            enviar_alertas_nuevos(cfg, nuevos)
        else:
            log("Sin cupos nuevos respecto a la corrida anterior")
        try:
            ultimo_resumen = datetime.fromisoformat(prev.get("ultimo_resumen"))
        except (TypeError, ValueError):
            ultimo_resumen = datetime.min
        if forzar_resumen or (ahora - ultimo_resumen) >= timedelta(minutes=cfg.resumen_min):
            enviar_resumen(cfg, agenda)
            ultimo_resumen = ahora

    guardar_estado(agenda, ultimo_resumen)
    sys.exit(0)


if __name__ == "__main__":
    main()
