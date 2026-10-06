# Auditoria de price-watch — 5 de septiembre de 2026

## Finalidad y arquitectura

El objetivo es detectar rebajas excepcionales en perfumes, hardware y celulares
de tiendas chilenas, y avisar por Telegram. No es un buscador general de precios
ni comprueba el checkout: se basa en precios publicados.

Hay tres rutas complementarias:

- `run.py`: barre el catalogo de Solotodo y compara el minimo agregado por
  producto con su propia historia de 30 dias. Confirma una entidad disponible
  antes de enviar.
- `run_capas.py`: construye una watchlist por entidad (tienda + SKU) usando
  historia de Solotodo y consulta directamente las fichas. Mediana y p10 filtran
  caidas habituales; las ventanas y umbrales dependen del perfil.
- `vigilar.py` / `francotirador.py`: acumulan precios por tienda para analizar
  movimientos de semanas, incluidas posibles alzas anteriores a promociones.

SQLite conserva tramos constantes; Git versiona las bases. Un sidecar con
actions/cache conserva la watchlist y validadores HTTP. El disparo previsto
combina Cloudflare con el cron de Actions; Telegram es el destino de alertas.

La separacion catalogo/historia/consulta directa es aprovechable. La debilidad
principal estaba en convertir cualquier numero plausible en una observacion
confiable. Optimizar concurrencia o aflojar umbrales antes de resolverlo aumenta
el volumen de errores.

## Alcance y evidencia

- Codigo local contrastado con `origin/main` en `dab3c55`; los cambios remotos
  desde la copia local eran solo historiales binarios.
- 133 pruebas originales pasaron antes de editar.
- Bases abiertas en modo de solo lectura: 5.550 productos, 112.061 tramos
  agregados, 9.104 tramos por tienda y 342 alertas. Son conteos de filas,
  no una estimacion de cobertura efectiva ni de calidad.
- Ultima ejecucion de produccion consultada:
  [33985077913](https://github.com/NotKn0xx/price-watch/actions/runs/33985077913).
  Celulares: 17 errores 403 de 50 consultas, todos en Ripley. Hardware: cinco
  403 de Notebooksya y un 404 de Tecno Master entre 40 consultas.
- Las doce ejecuciones recientes consultadas eran `schedule`; no aparecia
  `repository_dispatch`. En Price watch se observaron inicios a las 14:32,
  16:56 y 18:46 UTC del 5 de septiembre. Eso contradice una operacion real
  cada diez minutos. No se consulto la cuenta de Cloudflare y no se atribuye
  todavia una causa concreta al Worker.
- Busqueda de patrones de tokens GitHub/Telegram en archivos de texto
  versionados actuales: sin coincidencias. No equivale a una auditoria del
  historial completo, secretos remotos, logs historicos o paquetes instalados.

## Problemas encontrados y cambios

| Prioridad | Problema y consecuencia | Tratamiento |
|---|---|---|
| Alta | Elegir el numero mas cercano al precio anterior mezcla accesorios, variantes, precios tachados y rebajas reales. | Extraccion por `Product`/`Offer`, URL cuando existe y oferta unica. La referencia ya no decide la identidad. |
| Alta | Recorrido recursivo de cualquier `price` y rescate por regex de JSON roto. Puede leer envio, cuotas o datos sin contexto. | No se recorren recomendaciones ni costes de envio; se rechaza JSON invalido y no se usa texto libre como precio. |
| Alta | Precio presente se interpretaba como stock. Tampoco se verificaba CLP. | Disponibilidad trivalente y moneda explicita. La capa rapida solo alerta con CLP e `InStock`/`LimitedAvailability`. Agotados, preventas y desconocidos no alertan. |
| Alta | Normalizacion quitaba signos y letras; `52990.0` podia volverse `529900`. | Parseo restringido de formatos chilenos, cero decimal y enteros finitos positivos. Rechaza signos, texto, fracciones CLP y entradas excesivas. |
| Alta | `run.py` enlazaba una tienda pero alertaba el precio agregado de otra lectura. | Recalcula anomalia y cooldown con el precio de la entidad seleccionada; mensaje y registro usan ese precio. Sigue siendo confirmacion via API, no checkout. |
| Alta | Un fallo de Telegram o falta de cupo quedaba silenciado por la huella de precio igual. | Reevalua lecturas validas incluso con 304/precio igual. `alerts` decide la deduplicacion. |
| Alta | ETag de una pagina ilegible congelaba el parser; validadores viejos sobrevivian a cambios de URL. | Cache solo de lecturas validas, asociada a URL; 200 reemplaza validadores, 304 conserva datos confirmados. Sidecar v3 invalida el estado anterior. |
| Alta | Excepciones de Telegram pueden incluir el token dentro de la URL. | Solo se registra el tipo de error o codigo HTTP. No se imprime URL ni cuerpo arbitrario. |
| Alta | URL externa y redirecciones sin validacion podian alcanzar destinos internos. Descarga sin limite. | Rechazo de IP no global, credenciales, esquemas/puertos no previstos; validacion DNS y de cada salto; redireccion limitada al mismo host o su alias www, sin degradar HTTPS; 5 MiB descomprimidos, timeouts y cierre de conexiones. Limites residuales abajo. |
| Media | Reintentos POST de Telegram pueden duplicar un envio aceptado antes de perderse la respuesta. | Sin repeticion automatica de POST por lectura/estado; requiere `ok: true`. No se promete entrega exactamente una vez. |
| Media | Dos candidatos del mismo producto podian alertar en el mismo lote. | Actualizacion inmediata del mapa de cooldown despues de cada envio confirmado. |
| Media | Recuperacion de conflictos de Git perdia las alertas ya enviadas. | Diario separado `.alertas-<perfil>.json`, reinsertado idempotentemente por `reaplicar.py`, con timestamp original. |
| Media | `percentil=0` se convertia en `1` por un `or 1.0`. | Cero se conserva y cuenta como caida. |
| Media | Calculo de precio normal sostenido contaba periodos sin stock. | Duracion limitada por la siguiente muestra real, incluida la muestra agotada. |
| Media | Concurrencia de historia compartia una Session mutable; una tienda podia recibir ocho consultas simultaneas. | Session de Solotodo por hilo, una ficha simultanea por dominio y cierre de sesiones de la capa rapida. |
| Media | Bloqueos o errores se repetian en cada ciclo. | Espera exponencial persistida por entidad, hasta seis horas, respetando un `Retry-After` mayor. No se eluden bloqueos. |
| Media | Lista vacia provocaba repetir la capa lenta cada disparo; referencia vieja podia durar indefinidamente. | Reintento de lenta separado por una hora; se suspende la lista con referencia de mas de 48 horas. |
| Media | La rotacion de historia dependia del contador rapido; muestreos periodicos podian repetir subconjuntos. | Contador independiente por refresco de historia. |
| Media | Capa rapida no aplicaba el piso de precio al nuevo valor. | Aplica `MIN_PRICE_CLP` tambien en la evaluacion directa. |
| Media | Excepciones absorbidas y `continue-on-error` podian dejar un workflow verde sin escaneo util. | Errores completos se propagan despues de guardar estado; paso final revisa resultados de los seis perfiles. Se agrega CI de codigo para push/PR sin secretos. |
| Media | Se afirmaba campana o caida aislada con percentiles antiguos y agrupaciones posiblemente distintas. | El mensaje ya no presenta esa comparacion como comprobacion actual. |

## Verificacion con fichas reales

Muestra de cinco URLs existentes en las bases, consultadas con
`python auditar_fichas.py --max 5`. El script solo lee SQLite, descarga fichas
publicas y escribe diagnostico local; no importa ni llama a Telegram.

| Ficha | SQLite, CLP | Lectura HTML, CLP | Resultado |
|---|---:|---:|---|
| Ripley, Samsung Galaxy S26 Ultra | 899.990 | 1.049.990 | Oferta estructurada, CLP, stock declarado |
| Ripley, Cacharel Noa 100 ml | 87.990 | 109.990 | Oferta estructurada, CLP, stock declarado |
| Winpy, Kensington Pro Fit KB680 | 36.480 | 36.480 | Meta de precio, CLP, stock declarado |
| Falabella, iPhone 16 | 829.990 | — | HTTP 403 |
| Falabella, Acqua 125 ml | 16.990 | — | HTTP 403 |

Se corrigio con esa evidencia el reconocimiento de referencias `mainEntity`
y `@graph` de Ripley: una referencia y la definicion del mismo producto no son
dos variantes. Las discrepancias con SQLite no se clasifican automaticamente
como errores: SQLite puede estar atrasado, o la ficha haber cambiado de variante
o condiciones. No se comprobo el checkout ni se estima una precision global con
cinco paginas. La muestra tampoco demuestra cobertura de las 30 tiendas.

Pruebas automaticas: 164 casos en Python 3.14 local al cierre de la implementacion,
incluidos 31 casos nuevos de normalizacion, producto/variante, moneda, stock,
vigencia, referencias JSON-LD, redirecciones, destinos internos, tamanos,
notificaciones, cooldown, reaplicacion y precio real del enlace. La CI agregada
ejecuta Python 3.12, igual que el workflow de escaneo.

## Pendientes ordenados por impacto

1. **Restablecer y medir el disparador real.** Revisar despliegue, cron, secreto
   y respuestas del Worker en Cloudflare. Medir intervalo real entre escaneos
   por perfil y edad de la ultima observacion; los horarios configurados no
   demuestran que se ejecuten. No sustituir un token ni redesplegar sin verificar
   el estado de esa cuenta. Si ambos cron funcionan, evitar escaneos duplicados.
2. **Identidad completa de la oferta.** Persistir SKU/GTIN de la tienda,
   variante (ml, RAM, capacidad, color), vendedor y condicion; el product_id
   agregado sigue mezclando entidades y el cooldown sigue siendo por producto.
   El nuevo parser descarta ambiguedades, pero una URL unica no demuestra que
   la variante por defecto haya permanecido igual. Tampoco confirma precio
   exclusivo de tarjeta, membresia, despacho o disponibilidad por comuna.
3. **Cobertura y exactitud por tienda.** Crear fixtures etiquetadas de fichas
   comprables, agotadas, ofertas condicionadas, variantes y promociones. Adaptar
   cada tienda que no declara CLP/stock y comprobar cambios de plantilla. Para
   sitios que requieren JavaScript, evaluar un adaptador de navegador acotado
   solo si su acceso esta permitido; 403 no se soluciona aumentando concurrencia.
4. **Medir precision de alertas reales.** Etiquetar SKU correcto, precio visible,
   precio comprable, condicion y stock en el momento del aviso. Separar precision
   de extraccion, utilidad de la alerta, cobertura y latencia. El 87% del README
   agrupa etiquetas derivadas del percentil de toda la serie; no son etiquetas
   humanas de aciertos y las dudosas se incluyen como utiles. Reservar periodo
   temporal y tiendas fuera de calibracion; publicar tamanos de muestra e
   incertidumbre antes de cambiar umbrales. No se modificaron umbrales con base
   en esta muestra pequena.
5. **Historia disponible y madurez.** La capa lenta exige seis tramos, pero no
   una duracion minima suficiente; `timestamp_after` puede perder el tramo
   iniciado antes de la ventana. Hace falta recuperar una muestra anterior al
   borde, medir horas efectivamente observadas y caducidad por entidad. El
   historial agregado extiende el ultimo precio durante huecos sin observacion.
6. **Equidad de cobertura.** La rotacion independiente mejora la exploracion,
   pero reemplazar la watchlist con un subconjunto de 400 y priorizar por
   puntuacion puede dejar entidades poco atendidas. `entity_id % cadencia`
   puede concentrar mas de 50 entidades en el mismo turno. Programar por
   `next_due_at` y antiguedad, con limites por tienda y edad maxima observable.
7. **Persistencia fuera del historial de codigo.** SQLite comprimido es razonable
   para el volumen actual; guardar cada version binaria en Git acopla datos y
   despliegue. Separar almacenamiento durable, backups, observaciones con
   timestamp y migraciones. La recuperacion actual aun no reconstruye toda la
   metadata de productos ni sus timestamps originales. El diario de alertas
   reduce duplicados, pero quedan ventanas de fallo entre Telegram, disco y
   commit; una bandeja transaccional y reconciliacion harian explicito ese estado.
8. **Aislamiento y cadena de suministro.** La comprobacion DNS previa no elimina
   DNS rebinding entre resolver y conectar. Agregar politica de salida del
   runner y lista de dominios por tienda; los procesos de scraping deben tener
   el minimo acceso a secretos. Evitar HTTP cuando la tienda soporte HTTPS.
   Fijar dependencias con hashes, revisar vulnerabilidades con una herramienta
   dedicada, fijar Actions por SHA y separar el permiso `contents:write` del
   trabajo de extraccion. No se hizo una auditoria CVE completa en esta revision.
9. **Operacion observable.** Medir tasa 200/304/403/429, lecturas confirmadas,
   latencia, antiguedad de watchlist, ofertas suprimidas por motivo y calidad de
   alertas. Los contadores nuevos permiten detectar degradacion, pero no hay
   todavia un monitor externo que avise si ningun workflow llega a arrancar.
   El workflow `vigilar.yml` merece el mismo tratamiento de fallo parcial que
   se agrego al workflow principal.

## Efecto al activar los cambios

La cache v3 provoca un refresco inicial de las watchlists. La validacion estricta
puede reducir alertas y cobertura en tiendas con metadata incompleta: esos casos
deben aparecer como `sin_confirmacion` o un error concreto y medirse por tienda.
No se inventa stock para conservar el volumen anterior.

Los cambios de codigo y workflow se preparan en la rama
`codex/auditoria-precios-20260905`. Ninguna prueba local envio Telegram ni
modifico historiales de produccion. Fusionar el cambio activa la nueva conducta
en las siguientes corridas; no restaura por si solo el Worker de Cloudflare.

## Fuentes tecnicas

- [Schema.org Offer](https://schema.org/Offer): campos de oferta, moneda y disponibilidad.
- [Schema.org price](https://schema.org/price): el precio tambien puede ser un componente de coste, no necesariamente el total del producto.
- [OWASP SSRF Prevention](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html): validacion de destinos y redirecciones, y defensa de red.
- [Requests, uso avanzado](https://requests.readthedocs.io/en/stable/user/advanced/): sesiones, streaming, cierre y timeouts.

Las observaciones de produccion y codigo anteriores son evidencia de este
repositorio; las fuentes tecnicas sustentan el criterio de implementacion.
