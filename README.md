# Monitor de agenda

Bot que revisa periódicamente una web pública de reservas, lee los días
disponibles por profesional en las próximas 4 semanas y, si hay cupos, manda un
push al iPhone vía [ntfy.sh](https://ntfy.sh). Corre desatendido en GitHub
Actions (cron horario). No hay servidor vivo: cada corrida arranca, revisa y
termina.

Determinístico, sin LLM en runtime. Solo lectura: nunca reserva ni toca el
formulario de datos personales.

## Cómo funciona

`monitor.py` tiene tres funciones aisladas y testeables:

- `revisar_web(cfg)`: abre la web, hace la secuencia de clicks (categoría →
  intervención → continuar → profesional → continuar) para **cada** profesional
  y lee los días seleccionables del calendario.
- `amerita_aviso(estado)`: aplica la regla de negocio (¿hay cupos en la
  ventana?) y arma el mensaje.
- `enviar_aviso(topic, mensaje, ...)`: POST a ntfy (título, prioridad, tags).

## Notificaciones

El bot corre cada 5 min (cron) y persiste el estado entre corridas con el cache
de Actions (`estado.json`), para no repetir avisos:

- **Cupo nuevo** → apenas una corrida detecta un día que no estaba, manda una
  alerta **urgente** por servicio, con el servicio en el título. Solo avisa días
  que *aparecen* (no los que se ocupan).
- **Resumen horario** → cada `MONITOR_RESUMEN_MIN` (60 por defecto) manda sí o sí
  un resumen con todo lo disponible, aunque no haya cambios.
- **Primera corrida** (sin estado) → solo el resumen base, sin alertas, para no
  disparar todo lo disponible de golpe.

## Configuración (variables de entorno)

Todas son **secretos** salvo las dos opcionales. Los selectores del widget viven
en el código (son clases genéricas, no sensibles); lo sensible es la URL y qué
servicio se consulta.

| Variable | Secreto | Descripción |
|----------|:------:|-------------|
| `MONITOR_URL` | sí | URL de la agenda a revisar |
| `MONITOR_FLOWS` | sí | Lista JSON de flujos: `[{"cat","int","label"}]` |
| `NTFY_TOPIC` | sí | Nombre del topic de ntfy (funciona como secreto) |
| `MONITOR_SEMANAS` | no | Ventana en semanas (default `4`) |
| `MONITOR_RESUMEN_MIN` | no | Minutos entre resúmenes forzados (default `60`) |
| `MONITOR_HEADLESS` | no | `0` para ver el navegador local (default `1`) |

Cada flujo de `MONITOR_FLOWS` revisa una combinación categoría + intervención y,
dentro, todos los profesionales. `cat` e `int` son los `data-testid`; `label` es
una etiqueta **neutra** para la notificación. **No poner info de salud en
`label`** (el topic de ntfy no es privado): usar `Servicio 1`, `Servicio 2`, etc.
y guardar el mapeo real fuera del repo. Los profesionales se muestran como
`Profesional 1`, `Profesional 2` (nunca el nombre real).

## Correr local

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# Copiar .env.example a .env, completar, y cargarlo:
cp .env.example .env      # editar .env con URL, flujos y topic reales
set -a && source .env && set +a

python monitor.py
```

### Certificados en Mac (solo local)

Si al enviar el push local sale `CERTIFICATE_VERIFY_FAILED`, es que el Python de
python.org no tiene los certs raíz. Se resuelve con `pip install certifi` dentro
del venv (el código lo detecta y lo usa) o corriendo una vez
`/Applications/Python 3.13/Install Certificates.command`. En GitHub Actions
(Ubuntu) no aplica.

## Configurar ntfy en el iPhone

1. Instalar la app **ntfy** (App Store, gratis).
2. Suscribirse a un topic con nombre largo e impredecible, ej.
   `maximo-agenda-<string-aleatorio>`. **El nombre del topic ES el secreto:**
   quien lo conozca puede leer y publicar. Por eso va en Secrets, no en el código.

## Configurar GitHub Actions

1. **Settings → Secrets and variables → Actions → Secrets**, crear:
   `MONITOR_URL`, `MONITOR_FLOWS`, `NTFY_TOPIC`.
2. En la pestaña **Actions**, correr el workflow a mano con **Run workflow**
   (`workflow_dispatch`) para la primera prueba.
3. El cron horario queda activo solo.

## TODO de Máximo (no inventados por el bot)

- [ ] `MONITOR_URL` real.
- [ ] Confirmar `MONITOR_CATEGORIA_ID` / `MONITOR_INTERVENCION_ID` reales.
- [ ] **Fase 2 (headless=False):** validar en vivo los selectores de los
      desplegables, la navegación entre profesionales y el fechado de los días
      del mes siguiente (ver comentarios `FLAG FASE 2` en `monitor.py`).
- [ ] Definir el nombre del topic de ntfy.

## Fuera de alcance (v1)

- Sin login ni CAPTCHA (la web es pública).
- Sin persistencia entre corridas: cada run avisa los cupos actuales. Si se
  quiere avisar solo ante *cambios*, se agrega estado (Gist/artifact) después.
- Sin datos sensibles en el push (el topic de ntfy no es privado).
