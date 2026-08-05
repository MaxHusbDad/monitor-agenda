# SPEC: Bot de monitoreo de agenda con aviso push

Especificación para implementar con Claude Code. Determinística, desatendida,
gratis, sin dependencia de LLM en runtime.

---

## 1. Objetivo

Un bot que revisa periódicamente una web pública, ejecuta unos pasos fijos
(apretar un par de botones), lee el estado de una agenda, y si se cumple una
condición definida, envía una notificación push al iPhone vía ntfy.sh.

Corre desatendido en GitHub Actions mediante un workflow programado (cron).
No hay servidor vivo: cada corrida arranca, ejecuta y termina.

---

## 2. Stack decidido (no reabrir estas decisiones)

- **Lenguaje:** Python 3.11+
- **Automatización web:** Playwright (Chromium headless)
- **Hosting / scheduler:** GitHub Actions con `schedule: cron`
- **Notificación:** ntfy.sh (push al iPhone, sin cuenta ni token)
- **Frecuencia objetivo:** cada hora (ajustable, ver §7)

Descartado deliberadamente: agente de navegador / LLM en el loop (los pasos son
fijos), VM 24/7 (innecesaria para un job efímero), correo SMTP (ntfy es más
simple).

---

## 3. Comportamiento funcional

En cada corrida el bot debe:

1. Abrir la URL objetivo con Playwright, esperando a que cargue la red (`networkidle`).
2. Ejecutar la secuencia fija de clicks para llegar a la agenda.
3. Leer el contenido del contenedor de la agenda como texto.
4. Evaluar la regla de negocio: ¿amerita aviso o no?
5. Si amerita, publicar un mensaje en el topic de ntfy. Si no, terminar en silencio.
6. Loguear cada paso (a stdout, para que quede en los logs de Actions).
7. Salir con código 0 si todo corrió bien, 1 si falló la navegación (para que
   Actions marque el run como fallido y sea visible).

---

## 4. Estructura de archivos a generar

```
.
├── monitor.py                    # Script principal
├── requirements.txt              # playwright
├── .github/
│   └── workflows/
│       └── monitor.yml           # Workflow programado
├── .gitignore                    # Ignorar .env, __pycache__, *.log
└── README.md                     # Setup e instrucciones
```

---

## 5. Detalle de `monitor.py`

Tres funciones separadas, testeables de forma independiente:

- `revisar_web() -> dict`
  Abre la web, ejecuta los clicks, devuelve `{"ok": bool, "agenda": str}` o
  `{"ok": False, "error": str}` en caso de timeout. Usar selectores por rol/texto
  (`get_by_role`, `get_by_text`) por sobre XPaths, son más estables.

- `amerita_aviso(estado: dict) -> tuple[bool, str]`
  Recibe el estado, aplica la regla de negocio, devuelve `(bool, mensaje)`.
  Toda la lógica de "qué es un aviso" vive acá, aislada.

- `enviar_aviso(mensaje: str) -> None`
  POST a `https://ntfy.sh/<TOPIC>` con el mensaje en el body. Incluir header
  `Title` y `Priority`. El topic se lee desde variable de entorno, nunca hardcoded
  (ver §8, importante aunque el repo sea público).

Configuración por variables de entorno, leídas al inicio:
- `MONITOR_URL` — URL a revisar
- `NTFY_TOPIC` — nombre del topic de ntfy

Logging a stdout con timestamp y nivel. Sin archivos de log persistentes (el
runner es efímero; los logs quedan en la UI de Actions).

### Ejemplo del envío ntfy (referencia)

```python
import os, urllib.request

def enviar_aviso(mensaje: str) -> None:
    topic = os.environ["NTFY_TOPIC"]
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}",
        data=mensaje.encode("utf-8"),
        headers={"Title": "Monitor de agenda", "Priority": "high"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=10)
```

Usar `urllib` de la stdlib para no sumar dependencias; no hace falta `requests`.

---

## 6. Detalle de `monitor.yml`

```yaml
name: monitor-agenda

on:
  schedule:
    - cron: "0 * * * *"        # cada hora en punto (ajustar, ver §7)
  workflow_dispatch:            # permite gatillarlo a mano para probar

jobs:
  revisar:
    runs-on: ubuntu-latest
    timeout-minutes: 5          # corta si algo se cuelga
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          cache: pip
      - run: pip install -r requirements.txt
      - run: playwright install --with-deps chromium
      - name: Ejecutar monitor
        env:
          MONITOR_URL: ${{ vars.MONITOR_URL }}
          NTFY_TOPIC: ${{ secrets.NTFY_TOPIC }}
        run: python monitor.py
```

Notas:
- `workflow_dispatch` es clave para poder probar el bot manualmente desde la
  pestaña Actions sin esperar al cron.
- `--with-deps` instala las librerías de sistema que Chromium necesita en el runner.
- `timeout-minutes: 5` evita que un cuelgue queme minutos de cuota.

---

## 7. Frecuencia y cuota (repo privado)

> **FLAG — decisión pendiente de Máximo:** confirmar frecuencia final.

La cuota del plan Free en repo privado es 2.000 minutos Linux/mes, se resetea
cada mes. Cada corrida factura ~2-4 min (arranque + checkout + playwright install).

Recomendación: **cada hora** (`cron: "0 * * * *"`), 24 corridas/día, ~720/mes.
Deja margen amplio dentro de la cuota incluso si cada corrida dura 4 min.

Si se necesita más frecuencia, el mínimo del cron de GitHub es 5 min, pero a esa
cadencia el repo privado se pasa de cuota. Alternativas: hacer el repo público
(cron ilimitado gratis) o migrar a Cloud Run. No aplica salvo que cambie el
requisito.

El cron de GitHub Actions puede retrasarse varios minutos en horas peak. Si el
aviso llegara a ser time-critical, reconsiderar el hosting.

---

## 8. Configuración de GitHub (paso a paso para el README)

1. Instalar la app **ntfy** en el iPhone (App Store, gratis).
2. En la app, suscribirse a un topic con nombre largo e impredecible, ej.
   `maximo-agenda-<string-aleatorio>`. Este nombre funciona como secreto: quien
   lo conozca puede leer y publicar en él.
3. En el repo: **Settings → Secrets and variables → Actions**
   - En **Variables**, crear `MONITOR_URL` con la URL objetivo.
   - En **Secrets**, crear `NTFY_TOPIC` con el nombre del topic.
4. Verificar que el workflow aparece en la pestaña **Actions** y gatillarlo con
   **Run workflow** (`workflow_dispatch`) para la primera prueba.

> **Importante sobre repo público:** aunque ntfy no use credenciales tradicionales,
> el nombre del topic ES el secreto. Por eso va en **Secrets**, no en el código.
> Si el repo es público y el topic estuviera hardcodeado, cualquiera podría leer
> tus avisos o spammearte. La URL y los selectores sí pueden ir en el código
> (son web pública, no sensibles).

---

## 9. Contexto que Máximo debe completar (no inventar)

Claude Code debe dejar estos puntos como TODO explícitos, no rellenarlos con
supuestos:

- **`MONITOR_URL`**: la URL real. (No está en este spec.)
- **Secuencia de clicks en `revisar_web()`**: qué botones y en qué orden. Obtener
  los selectores reales con F12 sobre la web. Los del esqueleto son placeholders.
- **Selector del contenedor de la agenda**: dónde vive el texto a leer.
- **Regla de negocio en `amerita_aviso()`**: qué condición dispara el aviso
  (ej. aparece un cupo, cambia un estado, aparece una palabra clave).
- **Nombre del topic ntfy**: lo define Máximo, va en Secrets.

---

## 10. Criterios de aceptación

- [ ] `python monitor.py` corre local con `MONITOR_URL` y `NTFY_TOPIC` seteadas.
- [ ] Con `headless=False` local, se ve el navegador ejecutando los clicks correctos.
- [ ] Cuando la condición se cumple, llega un push al iPhone en segundos.
- [ ] Cuando no se cumple, el script termina sin enviar nada y loguea "sin novedad".
- [ ] `workflow_dispatch` ejecuta el bot exitosamente desde la UI de Actions.
- [ ] El cron dispara automáticamente en la cadencia configurada.
- [ ] Ningún secreto (topic) aparece en el código, en el `.yml` ni en los logs.

---

## 11. Fuera de alcance

- No usar LLM ni agente de navegador en runtime.
- No manejar CAPTCHA ni login (la web es pública y abierta).
- No persistir estado entre corridas en esta versión (cada run es independiente).
  Si más adelante se quiere avisar solo ante *cambios*, se agrega persistencia
  del último estado (ej. un artifact o Gist), pero no es parte de este spec.
- No enviar datos sensibles en el cuerpo del push (el topic ntfy no es privado).

---

## 12. Sugerencia de flujo con Claude Code

1. Iniciar el repo y correr Claude Code con este SPEC.md como contexto.
2. Pedirle que genere los 5 archivos de §4 respetando §5 y §6.
3. Completar tú los TODO de §9 con los datos reales de la web.
4. Probar local con `headless=False`, ajustar selectores.
5. Push, configurar Secrets/Variables (§8), probar con `workflow_dispatch`.
6. Activar el cron y validar una corrida automática.
