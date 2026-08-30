# Gedar RSS

Generador no oficial de un único RSS para seguir Gedar.

Fuentes:
- `https://gedar.eus/bilatzailea` (contenido editorial)
- `https://gedar.eus/telebista`
- `https://gedar.eus/agenda`

GitHub Actions ejecuta el generador dos veces por hora. El RSS resultante queda en `feed.xml`.

URL final:

`https://raw.githubusercontent.com/gorkalizari-spec/gedar-rss/main/feed.xml`

Primera ejecución:

`Actions → Actualizar RSS de Gedar → Run workflow`

Si el push automático fuese bloqueado:

`Settings → Actions → General → Workflow permissions → Read and write permissions`
