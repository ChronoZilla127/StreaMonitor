# CamSoda HLS: investigar cortes en grabaciones

En el modo custom de CamSoda, algunas playlists dejan de entregar segmentos nuevos aunque el canal siga figurando en línea. Tras 60 segundos sin video nuevo, StreaMonitor guarda lo recibido y puede iniciar otro intento. Ante fallos HTTP transitorios puede cambiar de playlist dentro de la misma grabación.

En la revisión del 13 de noviembre, FFprobe reconoció video y audio en los 13 MP4 nuevos. El registro mostró cinco saltos en las marcas de tiempo de las playlists. El mayor fue de 5,46 segundos en una grabación que continuó y se guardó. Queda pendiente medir cuánto contenido faltó en el MP4.

- [ ] Comparar un HAR y el registro de StreaMonitor del mismo directo para ubicar dónde dejaron de llegar segmentos nuevos.
- [ ] Medir los huecos con `EXT-X-PROGRAM-DATE-TIME` y contrastarlos con los segmentos guardados.
- [ ] Evaluar si conviene cambiar de playlist cuando deja de avanzar y reintentar los segmentos que siguen disponibles.
- [ ] Verificar que la recuperación conserva el orden, evita duplicados y permite cerrar la grabación limpiamente.
