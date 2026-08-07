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
    cadencia_min: int      # cada cuántos min corre el bot (ventana del resumen horario)


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
        cadencia_min=int(os.environ.get("MONITOR_CADENCIA_MIN", "5")),
    )


# --------------------------------------------------------------------------- #
# Navegación
# --------------------------------------------------------------------------- #
def _esperar_sin_overlay(page, timeout_ms: int = 15_000) -> None:
    """Espera a que el overlay de carga (vue-loading-overlay) deje de ser visible.

    La web tapa la pantalla mientras trae datos y ese overlay intercepta los
    clicks. Ojo: la clase `.vld-overlay.is-active` queda SIEMPRE en el DOM; en
    reposo el overlay está con `display:none` (w=0,h=0). Por eso hay que chequear
    visibilidad real (display + tamaño), no la presencia de la clase."""
    try:
        page.wait_for_function(
            "() => ![...document.querySelectorAll('.vld-overlay.is-active')]"
            ".some(e => getComputedStyle(e).display !== 'none'"
            " && e.getBoundingClientRect().width > 0)",
            timeout=timeout_ms,
        )
    except PlaywrightTimeoutError:
        pass


def _abrir_select(page, etiqueta: str) -> None:
    """Abre un desplegable stf-select por el texto de su placeholder y espera
    a que sus opciones queden visibles.

    Los 3 selects tienen su contenedor de opciones en el DOM siempre; el abierto
    es el único con `visibility: visible`. Por eso filtramos con `:visible`
    (Playwright respeta visibility:hidden), que aísla el dropdown abierto.
    """
    _esperar_sin_overlay(page)
    page.locator(".stf-select", has_text=etiqueta).locator(SEL_VALUE).first.click()
    page.locator(f"{SEL_OPCION}:visible").first.wait_for(timeout=8_000)


def _click_continuar(page) -> None:
    _esperar_sin_overlay(page)
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


def _leer_horas_de_dia(page, cel_libre) -> list[str]:
    """Clickea un día disponible y devuelve sus horas (`.custom-timeslot-border`).

    Las horas de un día solo se renderizan al seleccionarlo. Best-effort: si
    algo falla, devuelve [] (el día igual se reporta, sin horas)."""
    try:
        cel_libre.click()
        _esperar_sin_overlay(page)
        # Los slots pueden cargar progresivamente: esperamos a que la cantidad
        # quede estable en dos lecturas seguidas (evita leer horas incompletas,
        # que darían falsas alertas de 'hora nueva').
        prev = -1
        for _ in range(6):
            page.wait_for_timeout(400)
            n = page.locator(SEL_SLOT).count()
            if n == prev:
                break
            prev = n
        slots = page.locator(SEL_SLOT)
        horas = set()
        for j in range(slots.count()):
            txt = (slots.nth(j).inner_text() or "").strip()
            if txt:
                horas.add(txt)
        return sorted(horas)
    except Exception:
        return []


def _dias_disponibles_en_vista(page, mes: int, anio: int) -> dict:
    """Devuelve {fecha: [horas]} de las celdas seleccionables del calendario.

    Disponible = celda con `cursor-pointer` y SIN `cursor-not-allowed`. El widget
    muestra días del mes siguiente como seleccionables con el dropdown aún en el
    mes actual; el fechado usa rollover cuando el número de día baja tras fin de
    mes. Se hace en dos fases para no invalidar los locators: primero se fechan
    todas las celdas disponibles (solo lectura), luego se clickea cada una para
    leer sus horas.
    """
    # Fase 1: fechar cada celda disponible, en orden de aparición
    fechas_disp: list[date | None] = []
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
        if "cursor-pointer" in clases and "cursor-not-allowed" not in clases:
            try:
                fechas_disp.append(date(a, m, dia))
            except ValueError:
                fechas_disp.append(None)   # mantiene alineación con SEL_DIA_LIBRE
    # Fase 2: click en cada día disponible y leer sus horas
    resultado: dict[date, list[str]] = {}
    libres = page.locator(SEL_DIA_LIBRE)
    for i in range(min(libres.count(), len(fechas_disp))):
        fecha = fechas_disp[i]
        if fecha is None:
            continue
        resultado[fecha] = _leer_horas_de_dia(page, libres.nth(i))
    return resultado


def _avanzar_mes(page) -> bool:
    """Intenta cambiar a otro mes en el selector. Best-effort y NO-FATAL.

    El dropdown de mes carga las opciones de forma lazy y lista únicamente los
    meses CON disponibilidad para ese profesional. Al abrirlo suele haber solo el
    mes actual y un placeholder oculto "Cargando más opciones..." que no resuelve
    (ni con scroll ni con búsqueda). Por eso: abrimos, y solo clickeamos una
    opción REAL, visible y de un mes distinto al actual. Si no hay tal opción, no
    hay nada que avanzar (no hay disponibilidad en otros meses) y devolvemos
    False. Cualquier error se traga y devuelve False para no romper la corrida.
    """
    try:
        actual = ""
        if page.locator(SEL_MES).count():
            actual = (page.locator(SEL_MES).first.inner_text() or "").strip()
        _esperar_sin_overlay(page)
        toggle = page.locator(f"{SEL_MES_WRAP} .vs__dropdown-toggle")
        if not toggle.count():
            return False
        toggle.first.click()
        page.wait_for_timeout(400)
        lis = page.locator(f"{SEL_MES_WRAP} ul[role=listbox] li")
        objetivo = None
        for i in range(lis.count()):
            li = lis.nth(i)
            if not li.is_visible():
                continue                     # descarta el placeholder oculto
            txt = (li.inner_text() or "").strip()
            if not txt or "Cargando" in txt or "No se han" in txt or txt == actual:
                continue                     # descarta placeholders y el mes actual
            objetivo = li
            break
        if objetivo is None:
            page.keyboard.press("Escape")
            return False
        objetivo.click()
        page.wait_for_timeout(500)
        _esperar_sin_overlay(page)
        return True
    except Exception:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False


SEL_DIA_LIBRE = f"{SEL_DIA}.cursor-pointer:not(.cursor-not-allowed)"
SEL_DIA_BLOQ = f"{SEL_DIA}.cursor-not-allowed"
SEL_SLOT = ".custom-timeslot-border"        # cada hora disponible de un día


def _esperar_calendario(page) -> None:
    """Espera a que la disponibilidad se asiente antes de leer.

    Al renderizar, el widget marca TODAS las celdas como disponibles y recién
    después aplica los días bloqueados desde el backend. Leer en ese instante
    daba bloques contiguos de días fantasma (falsos 'cupo nuevo'). Esperamos a
    que aparezca al menos un día bloqueado (señal de que la data se aplicó) y a
    que la cantidad de días disponibles quede estable en dos lecturas seguidas.
    """
    _esperar_sin_overlay(page)
    try:
        page.wait_for_selector(SEL_DIA_BLOQ, timeout=6_000)
    except PlaywrightTimeoutError:
        pass   # mes íntegramente disponible (raro): seguimos igual
    prev = -1
    for _ in range(6):
        page.wait_for_timeout(500)
        actual = page.locator(SEL_DIA_LIBRE).count()
        if actual == prev:
            return
        prev = actual


def _leer_agenda_calendario(page, hoy: date, fin: date, semanas: int) -> dict:
    """Lee {día: [horas]} del calendario, dentro de la ventana [hoy, fin]."""
    dias: dict[date, list[str]] = {}
    for _ in range(semanas // 4 + 2):   # tope de meses a revisar
        try:
            _esperar_calendario(page)   # evita leer días fantasma sin asentar
            mes, anio = _mes_dropdown(page)
            dias.update(_dias_disponibles_en_vista(page, mes, anio))
            if date(anio, mes, monthrange(anio, mes)[1]) >= fin:
                break
            if not _avanzar_mes(page):
                break
        except Exception as e:   # no propagar: devolvemos lo leído hasta acá
            log(f"Corte de lectura de calendario: {type(e).__name__}", "WARN")
            break
    return {f: dias[f] for f in sorted(dias) if hoy <= f <= fin}


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
            exitos = 0
            for fi, flow in enumerate(cfg.flows, start=1):
                label = flow["label"]
                try:
                    profs = listar_profesionales(page, cfg.url, flow["cat"], flow["int"])
                except Exception as e:   # un flujo caído no tumba al resto
                    log(f"Flujo {fi}: no se listaron profesionales ({type(e).__name__}); se omite", "WARN")
                    _dump_debug(page, f"flujo{fi}_listar")
                    continue
                por_prof: dict[str, list[date]] = {}
                for i, prof in enumerate(profs, start=1):
                    try:
                        _navegar_a_profesionales(page, cfg.url, flow["cat"], flow["int"])
                        _esperar_sin_overlay(page)
                        page.locator(f'{SEL_OPCION}[data-testid="{prof["testid"]}"]').click()
                        _click_continuar(page)
                        page.wait_for_selector(SEL_DIA, timeout=15_000)
                        dias = _leer_agenda_calendario(page, hoy, fin, cfg.semanas)
                        por_prof[f"Profesional {i}"] = dias   # nunca el nombre real
                        exitos += 1
                        # Log por índice, no por label: los logs de Actions son
                        # públicos y el label puede tener el nombre real del servicio.
                        log(f"Flujo {fi} / Profesional {i}: {len(dias)} día(s) en la ventana")
                    except Exception as e:   # un profesional caído se omite
                        log(f"Flujo {fi} / Profesional {i}: error ({type(e).__name__}); se omite", "WARN")
                        _dump_debug(page, f"flujo{fi}_prof{i}")
                if por_prof:
                    agenda[label] = por_prof
            if exitos == 0:
                return {"ok": False, "error": "no se pudo leer ningún profesional"}
            return {"ok": True, "agenda": agenda}
        finally:
            browser.close()


# --------------------------------------------------------------------------- #
# Regla de negocio y aviso
# --------------------------------------------------------------------------- #
def _fmt_fecha(f: date) -> str:
    return f"{DIAS_ABR[f.weekday()]} {f.day} {MESES_ABR[f.month]}"


def _fmt_dia_horas(f: date, horas: list) -> str:
    """'mar 25 ago — 11:00, 13:20' (o solo el día si no hay horas)."""
    base = _fmt_fecha(f)
    return f"{base} — {', '.join(horas)}" if horas else base


def _cuerpo_por_prof(por_prof: dict) -> str:
    """Arma el bloque de cada profesional con sus días y horas.

    Estructura de entrada: {'Profesional N': {fecha: [horas]}}.
    """
    lineas = []
    for prof, dias in por_prof.items():
        if not dias:
            continue
        lineas.append(f"{prof}:")
        for f in sorted(dias):
            lineas.append(f"  · {_fmt_dia_horas(f, dias[f])}")
    return "\n".join(lineas)


def _nuevos_cupos(prev_agenda: dict, agenda: dict) -> dict:
    """Devuelve {servicio: {'Profesional N': {fecha: [horas nuevas]}}} con SOLO
    los (día, hora) que aparecieron respecto a la corrida anterior. Un día nuevo
    entero trae todas sus horas; un día ya visto trae solo sus horas nuevas."""
    nuevos: dict[str, dict[str, dict]] = {}
    for svc, por_prof in agenda.items():
        prev_prof = (prev_agenda or {}).get(svc, {})
        if not isinstance(prev_prof, dict):
            prev_prof = {}
        for prof, dias in por_prof.items():
            prev_dias = prev_prof.get(prof, {})   # {fecha_iso: [horas]}
            if not isinstance(prev_dias, dict):
                # Estado en formato viejo (lista de fechas, sin horas). Lo
                # tratamos como baseline: no disparamos alertas en la corrida de
                # migración; se re-guarda en el formato nuevo para la próxima.
                continue
            dias_nuevos = {}
            for f, horas in dias.items():
                antes = set(prev_dias.get(f.isoformat(), []))
                agregadas = [h for h in horas if h not in antes]
                # Día nuevo sin horas legibles igual cuenta como cupo nuevo.
                if agregadas or (f.isoformat() not in prev_dias and not horas):
                    dias_nuevos[f] = agregadas
            if dias_nuevos:
                nuevos.setdefault(svc, {})[prof] = dias_nuevos
    return nuevos


def _agenda_a_json(agenda: dict) -> dict:
    return {svc: {prof: {f.isoformat(): horas for f, horas in dias.items()}
                  for prof, dias in por_prof.items()}
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


def guardar_estado(agenda: dict) -> None:
    with open(_ruta_estado(), "w", encoding="utf-8") as f:
        json.dump({"agenda": _agenda_a_json(agenda)}, f, ensure_ascii=False, indent=2)


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


def _toca_resumen_horario(ahora: datetime, cadencia_min: int) -> bool:
    """True en la única corrida por hora que cae cerca de la hora en punto.

    Con corridas cada `cadencia_min`, exactamente una por hora cae en el minuto
    [0, cadencia_min). Decidir por reloj (y no por un timestamp persistido) hace
    que el resumen salga 1 vez/hora aunque el cache de estado falle o no se
    restaure: nunca se dispara en cada corrida.
    """
    return ahora.minute < cadencia_min


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
    forzar = os.environ.get("MONITOR_FORZAR_RESUMEN") == "1"
    hay_prev = prev is not None
    toca = _toca_resumen_horario(ahora, cfg.cadencia_min)
    log(f"Estado previo: {'sí' if hay_prev else 'no'} | minuto={ahora.minute} | "
        f"toca_resumen={toca} | forzar={forzar}")

    # Alertas y resumen van blindados: un error acá (estado raro, ntfy caído)
    # NUNCA debe impedir guardar el estado, o el bot queda en crash-loop.
    try:
        if hay_prev:
            nuevos = _nuevos_cupos(prev.get("agenda", {}), agenda)
            if nuevos:
                enviar_alertas_nuevos(cfg, nuevos)
            else:
                log("Sin cupos nuevos respecto a la corrida anterior")
        else:
            log("Sin estado previo (primera corrida o cache no restaurado): sin alertas")

        # Resumen: SOLO 1 vez/hora (por reloj) o forzado manual. Nunca en cada corrida.
        if forzar or toca:
            enviar_resumen(cfg, agenda)
        else:
            log("No corresponde resumen en esta corrida")
    except Exception as e:
        log(f"Error en alertas/resumen (no bloquea el guardado): {type(e).__name__}: {e}", "ERROR")

    guardar_estado(agenda)
    sys.exit(0)


if __name__ == "__main__":
    main()
